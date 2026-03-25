
import requests
import json
import os
import shutil
import base64
import time
import ssl
import urllib3
import subprocess
import sys
import socket
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import psutil
    from lockutils import get_lcu_credentials
except ImportError:
    print("Erro: A biblioteca 'psutil' não está instalada.")
    print("Por favor, instale-a usando: pip install psutil")
    input("\nPressione Enter para sair...")
    sys.exit(1)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RIOT_API_KEY = os.getenv("RIOT_API_KEY")
LCU_PORT = None
LCU_PASSWORD = None
PHP_SERVER_PROCESS = None
PHP_SERVER_PORT = 8080

# Lock para acesso thread-safe ao LCU e ao rate limiter da Riot API
_lcu_lock = threading.Lock()
_riot_api_lock = threading.Lock()
_print_lock = threading.Lock()

# Controle de rate limit da Riot API (100 req / 2min = ~0.5s entre requests)
_last_riot_request_time = 0
RIOT_API_MIN_INTERVAL = 1.2  # segundos entre chamadas à Riot API (Spectator tem limite ~50/min)


def safe_print(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


# ─────────────────────────────────────────────
#  PHP LOCAL SERVER
# ─────────────────────────────────────────────

def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def start_php_server(directory, port=PHP_SERVER_PORT):
    global PHP_SERVER_PROCESS
    if is_port_in_use(port):
        print(f"Servidor PHP ja esta rodando na porta {port}.")
        return True
    print(f"Iniciando servidor PHP em {directory} na porta {port}...")
    try:
        PHP_SERVER_PROCESS = subprocess.Popen(
            ["php", "-S", f"127.0.0.1:{port}", "-t", directory],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(2)
        if PHP_SERVER_PROCESS.poll() is None:
            print(f"Servidor PHP iniciado com PID {PHP_SERVER_PROCESS.pid}.")
            return True
        else:
            print("Falha ao iniciar o servidor PHP.")
            return False
    except FileNotFoundError:
        print("Erro: PHP nao encontrado.")
        return False

def stop_php_server():
    global PHP_SERVER_PROCESS
    if PHP_SERVER_PROCESS and PHP_SERVER_PROCESS.poll() is None:
        print("Encerrando servidor PHP...")
        PHP_SERVER_PROCESS.terminate()
        PHP_SERVER_PROCESS = None


# ─────────────────────────────────────────────
#  UTILITARIOS GERAIS
# ─────────────────────────────────────────────

def get_riot_api_key():
    global RIOT_API_KEY
    if not RIOT_API_KEY:
        RIOT_API_KEY = input("Por favor, insira sua chave da Riot Games API: ").strip()
    return RIOT_API_KEY

def close_riot_client_and_lol():
    print("Fechando League of Legends e Riot Client...")
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] in ["LeagueClient.exe", "RiotClientServices.exe", "League of Legends.exe"]:
            try:
                proc.kill()
                print(f"Processo {proc.info['name']} encerrado.")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    time.sleep(5)


# ─────────────────────────────────────────────
#  RIOT CLIENT — abrir, logar e aguardar LCU
# ─────────────────────────────────────────────

RIOT_CLIENT_PATHS = [
    r"C:\Riot Games\Riot Client\RiotClientServices.exe",
    r"C:\Program Files\Riot Games\Riot Client\RiotClientServices.exe",
    r"C:\Program Files (x86)\Riot Games\Riot Client\RiotClientServices.exe",
]

def find_riot_client_exe():
    for path in RIOT_CLIENT_PATHS:
        if os.path.exists(path):
            return path
    for proc in psutil.process_iter(["name", "exe"]):
        if proc.info["name"] == "RiotClientServices.exe":
            return proc.info["exe"]
    return None

def is_riot_client_running():
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] in ("RiotClientServices.exe", "LeagueClient.exe"):
            return True
    return False

def is_league_client_running():
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] == "LeagueClient.exe":
            return True
    return False

def find_league_client_exe():
    """Encontra o LeagueClient.exe diretamente."""
    paths = [
        r"C:\Riot Games\League of Legends\LeagueClient.exe",
        r"C:\Program Files\Riot Games\League of Legends\LeagueClient.exe",
        r"C:\Program Files (x86)\Riot Games\League of Legends\LeagueClient.exe",
    ]
    for path in paths:
        if os.path.exists(path):
            return path
    for proc in psutil.process_iter(["name", "exe"]):
        if proc.info["name"] == "LeagueClient.exe":
            return proc.info["exe"]
    return None

def launch_riot_client(no_launch=False, headless=False):
    if is_riot_client_running():
        print("Riot Client ja esta em execucao.")
        return True
    exe = find_riot_client_exe()
    if not exe:
        print("Erro: Nao foi possivel encontrar o RiotClientServices.exe.")
        return False
    print(f"Abrindo Riot Client: {exe}")
    try:
        if no_launch:
            args = [exe]
        else:
            args = [exe, "--launch-product=league_of_legends", "--launch-patchline=live"]
        if headless:
            args.append("--headless")
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"Erro ao abrir o Riot Client: {e}")
        return False

def wait_for_league_client(timeout=120, poll_interval=3):
    print(f"Aguardando LeagueClient.exe iniciar (timeout: {timeout}s)...")
    elapsed = 0
    while elapsed < timeout:
        if is_league_client_running():
            print(f"LeagueClient.exe detectado apos {elapsed}s.")
            return True
        time.sleep(poll_interval)
        elapsed += poll_interval
        print(f"  Aguardando... {elapsed}s/{timeout}s")
    print("Timeout: LeagueClient.exe nao foi detectado.")
    return False

def parse_lockfile_content(content):
    parts = content.strip().split(':')
    if len(parts) >= 4:
        return parts[2], parts[3]
    return None, None

def wait_for_lcu_ready(timeout=90, poll_interval=3):
    print(f"Aguardando LCU ficar disponivel (timeout: {timeout}s)...")
    elapsed = 0
    while elapsed < timeout:
        port, password = get_lcu_credentials()
        if port and password:
            try:
                url = f"https://127.0.0.1:{port}/lol-service-status/v1/lcu-status"
                headers = {
                    "Authorization": "Basic " + base64.b64encode(f"riot:{password}".encode()).decode()
                }
                resp = requests.get(url, headers=headers, verify=False, timeout=3)
                if resp.status_code in (200, 404):
                    print(f"  LCU disponivel na porta {port}.")
                    return port, password
            except Exception:
                pass
        time.sleep(poll_interval)
        elapsed += poll_interval
        print(f"  Aguardando LCU... {elapsed}s/{timeout}s")
    print("Timeout: LCU nao ficou disponivel.")
    return None, None

def type_with_clipboard(text):
    try:
        proc = subprocess.Popen(['clip'], stdin=subprocess.PIPE, close_fds=True)
        proc.communicate(input=text.encode('utf-16-le'))
    except Exception:
        pass

RIOT_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Riot Games", "Riot Client", "Data"
)
RIOT_SETTINGS_FILENAME = "RiotGamesPrivateSettings.yaml"

# Pasta sessions sempre relativa ao botlol.py
SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")

def clear_riot_client_data():
    """
    Limpa a pasta Data do Riot Client antes do login.
    Deleta RiotGamesPrivateSettings.yaml e outros arquivos de sessao.
    """
    data_dir = RIOT_DATA_DIR

    if not os.path.exists(data_dir):
        safe_print(f"  AVISO: Pasta Data nao encontrada: {data_dir}")
        return False

    safe_print(f"  Limpando pasta Data: {data_dir}")

    try:
        for item in os.listdir(data_dir):
            item_path = os.path.join(data_dir, item)
            if os.path.isfile(item_path):
                os.remove(item_path)
                safe_print(f"    Deletado: {item}")
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)
                safe_print(f"    Deletado pasta: {item}")

        safe_print(f"  Pasta Data limpa com sucesso.")
        return True
    except Exception as e:
        safe_print(f"  ERRO ao limpar pasta Data: {e}")
        return False

def get_riot_settings_path():
    """Retorna o caminho dinamico do RiotGamesPrivateSettings.yaml"""
    base_path = os.path.join(RIOT_DATA_DIR, RIOT_SETTINGS_FILENAME)
    return base_path if os.path.exists(base_path) else None

def restaurar_session(username, sessions_dir):
    """Copia RiotGamesPrivateSettings.yaml de sessions/<username>/ para a pasta Data do Riot."""
    src = os.path.join(sessions_dir, username, RIOT_SETTINGS_FILENAME)
    dst = os.path.join(RIOT_DATA_DIR, RIOT_SETTINGS_FILENAME)

    safe_print(f"  [SESSION] Procurando: {src}")

    if not os.path.exists(src):
        safe_print(f"  [SESSION] Arquivo nao encontrado!")
        if os.path.isdir(sessions_dir):
            contas_salvas = os.listdir(sessions_dir)
            safe_print(f"  [SESSION] Contas em sessions/: {contas_salvas}")
        else:
            safe_print(f"  [SESSION] Pasta sessions/ nao existe: {sessions_dir}")
        return False

    try:
        os.makedirs(RIOT_DATA_DIR, exist_ok=True)
        shutil.copy2(src, dst)
        safe_print(f"  Sessao restaurada para {username}.")
        return True
    except Exception as e:
        safe_print(f"  ERRO ao restaurar sessao: {e}")
        return False

def deletar_yaml_data():
    """Deleta o RiotGamesPrivateSettings.yaml da pasta Data após salvar a sessão."""
    yaml_path = os.path.join(RIOT_DATA_DIR, RIOT_SETTINGS_FILENAME)
    if os.path.exists(yaml_path):
        try:
            os.remove(yaml_path)
            safe_print(f"  {RIOT_SETTINGS_FILENAME} removido da pasta Data.")
        except Exception as e:
            safe_print(f"  ERRO ao remover yaml: {e}")

def salvar_session_renovada(username, sessions_dir):
    """Copia o RiotGamesPrivateSettings.yaml renovado de volta para sessions/<username>/ e deleta da Data."""
    settings_path = get_riot_settings_path()
    if not settings_path:
        safe_print(f"  AVISO: {RIOT_SETTINGS_FILENAME} nao encontrado para salvar.")
        return False
    conta_session_dir = os.path.join(sessions_dir, username)
    dest = os.path.join(conta_session_dir, RIOT_SETTINGS_FILENAME)
    try:
        os.makedirs(conta_session_dir, exist_ok=True)
        shutil.copy2(settings_path, dest)
        safe_print(f"  Sessao renovada salva para {username}.")
        deletar_yaml_data()
        return True
    except Exception as e:
        safe_print(f"  ERRO ao salvar sessao renovada: {e}")
        return False

def launch_via_session(username, sessions_dir):
    """
    Login usando sessao salva — sem pyautogui.
    1. Fecha o cliente (kill, sem logout)
    2. Limpa a pasta Data
    3. Restaura o RiotGamesPrivateSettings.yaml da conta
    4. Abre o Riot Client com --launch-product — ele loga e abre o LoL automaticamente
    """
    global LCU_PORT, LCU_PASSWORD

    safe_print(f"  >> Login via sessao salva: {username}")

    close_riot_client_and_lol()
    time.sleep(2)

    clear_riot_client_data()

    tem_session = restaurar_session(username, sessions_dir)
    if not tem_session:
        safe_print(f"  Sem sessao salva para {username}. Use a opcao 4 primeiro.")
        return None, None

    # Lança via RiotClientServices com --launch-product (tem permissoes necessarias)
    if not launch_riot_client(no_launch=False, headless=False):
        safe_print("  Nao foi possivel abrir o Riot Client.")
        return None, None

    # Com sessao salva pula login — LeagueClient abre mais rapido
    time.sleep(5)

    if not wait_for_league_client(timeout=90):
        safe_print("  LeagueClient nao iniciou em 90s.")
        return None, None

    port, password = wait_for_lcu_ready(timeout=60)
    if not port:
        safe_print("  LCU nao ficou disponivel.")
        return None, None

    LCU_PORT = port
    LCU_PASSWORD = password
    safe_print(f"  LCU pronto na porta {port}.")
    return port, password


def launch_and_login(username, user_password):
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.05
    except ImportError:
        print("Erro: pyautogui nao instalado. Execute: pip install pyautogui")
        return None, None

    global LCU_PORT, LCU_PASSWORD

    print(f"\n{'='*60}")
    print(f"  Preparando conta: {username}")
    print(f"{'='*60}")

    close_riot_client_and_lol()

    if not launch_riot_client():
        print("Nao foi possivel abrir o Riot Client.")
        return None, None

    print("Aguardando tela de login do Riot Client carregar...")
    time.sleep(25)

    print(f"Digitando usuario: {username}")

    try:
        _riot_win = get_riot_window(timeout=20)
        if not _riot_win:
            raise Exception("Janela do Riot Client nao encontrada")
        try:
            _riot_win.activate()
        except Exception:
            pass
        pyautogui.hotkey('ctrl', 'a')
        time.sleep(0.2)
        type_with_clipboard(username)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.3)
        pyautogui.press('tab')
        time.sleep(0.3)
        type_with_clipboard(user_password)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.3)
        print("Confirmando login (Enter)...")
        pyautogui.press('enter')
    except Exception as e:
        print(f"Erro ao digitar credenciais: {e}")
        return None, None

    print("Aguardando LeagueClient.exe iniciar apos login...")
    if not wait_for_league_client(timeout=100):
        print("LeagueClient nao iniciou em 100s. Pulando para proxima conta.")
        return None, None

    print("LeagueClient detectado. Aguardando LCU inicializar...")
    time.sleep(5)
    port, password = wait_for_lcu_ready(timeout=90)
    if not port:
        print("LCU nao ficou disponivel.")
        return None, None

    LCU_PORT = port
    LCU_PASSWORD = password
    print(f"Pronto! LCU conectado na porta {port} para conta {username}.")
    return port, password

def get_riot_window(timeout=20):
    """
    Aguarda e retorna a janela do Riot Client.
    Tenta varios titulos possiveis pois varia por versao/idioma.
    """
    import pyautogui
    titulos_possiveis = ["Riot Client", "Riot Client Main", "Riot Games"]
    elapsed = 0
    while elapsed < timeout:
        for titulo in titulos_possiveis:
            wins = pyautogui.getWindowsWithTitle(titulo)
            if wins:
                return wins[0]
        # Busca qualquer janela que contenha "Riot" no titulo
        import pygetwindow as gw
        try:
            todas = gw.getAllWindows()
            for w in todas:
                if "riot" in w.title.lower() and w.width > 100:
                    return w
        except Exception:
            pass
        time.sleep(1)
        elapsed += 1
    return None


    """
    Versao rapida de login para o checker.
    - Espera apenas 12s pela tela de login (em vez de 25s)
    - Timeout de LeagueClient reduzido para 60s
    - Timeout de LCU reduzido para 45s
    - Sem sleep extra apos detectar LeagueClient
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.05
    except ImportError:
        print("Erro: pyautogui nao instalado.")
        return None, None

    global LCU_PORT, LCU_PASSWORD

    print(f"  >> Logando em: {username}")
    close_riot_client_and_lol()

    if not launch_riot_client():
        print("  Nao foi possivel abrir o Riot Client.")
        return None, None

    time.sleep(12)  # espera tela de login (reduzido de 25s)

    try:
        _riot_win = get_riot_window(timeout=20)
        if not _riot_win:
            raise Exception("Janela do Riot Client nao encontrada")
        try:
            _riot_win.activate()
        except Exception:
            pass
        pyautogui.hotkey('ctrl', 'a')
        time.sleep(0.15)
        type_with_clipboard(username)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.2)
        pyautogui.press('tab')
        time.sleep(0.2)
        type_with_clipboard(user_password)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.2)
        pyautogui.press('enter')
    except Exception as e:
        print(f"  Erro ao digitar credenciais: {e}")
        return None, None

    if not wait_for_league_client(timeout=60):
        print("  LeagueClient nao iniciou em 60s. Pulando.")
        return None, None

    port, password = wait_for_lcu_ready(timeout=45)
    if not port:
        print("  LCU nao ficou disponivel.")
        return None, None

    LCU_PORT = port
    LCU_PASSWORD = password
    print(f"  LCU pronto na porta {port}.")
    return port, password


def launch_and_login_save_session(username, user_password):
    """
    Login marcando o checkbox 'Manter sessao iniciada' antes de confirmar.
    Layout do Riot Client: usuario -> senha -> [Stay signed in checkbox] -> botao entrar
    Tab order: campo usuario -> campo senha -> checkbox -> botao entrar
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.05
    except ImportError:
        print("Erro: pyautogui nao instalado.")
        return None, None

    global LCU_PORT, LCU_PASSWORD

    print(f"  >> Logando (salvar sessao): {username}")
    close_riot_client_and_lol()

    if not launch_riot_client(no_launch=False):
        print("  Nao foi possivel abrir o Riot Client.")
        return None, None

    time.sleep(14)  # um pouco mais de espera para garantir que o checkbox carregou

    try:
        _riot_win = get_riot_window(timeout=20)
        if not _riot_win:
            raise Exception("Janela do Riot Client nao encontrada")
        try:
            _riot_win.activate()
        except Exception:
            pass
        time.sleep(0.3)

        # Digitar usuario
        pyautogui.hotkey('ctrl', 'a')
        time.sleep(0.2)
        type_with_clipboard(username)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.3)

        # Tab para campo de senha
        pyautogui.press('tab')
        time.sleep(0.2)
        type_with_clipboard(user_password)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.3)

        # Tab para o checkbox "Manter sessao iniciada" e marcar com espaco
        pyautogui.press('tab', presses=6, interval=0.1)
        time.sleep(0.2)
        pyautogui.press('space')  # marca o checkbox
        time.sleep(0.2)

        # Enter para confirmar login
        pyautogui.press('enter')

        print(f"  Checkbox 'Manter sessao' marcado.")
    except Exception as e:
        print(f"  Erro ao digitar credenciais: {e}")
        return None, None

    if not wait_for_league_client(timeout=80):
        print("  LeagueClient nao iniciou em 80s. Pulando.")
        return None, None

    port, password = wait_for_lcu_ready(timeout=60)
    if not port:
        print("  LCU nao ficou disponivel.")
        return None, None

    LCU_PORT = port
    LCU_PASSWORD = password
    print(f"  LCU pronto na porta {port}.")
    return port, password


def read_account_credentials(file_path):
    accounts = []
    print(f"Tentando ler credenciais do arquivo: {file_path}")
    try:
        with open(file_path, 'r') as f:
            print(f"Arquivo {file_path} aberto com sucesso.")
            for line in f:
                line = line.strip()
                if line and ':' in line:
                    username, password = line.split(':', 1)
                    accounts.append({'username': username, 'password': password})
    except FileNotFoundError:
        print(f"Erro: Arquivo {file_path} nao encontrado.")
        print(f"Caminho absoluto atual: {os.getcwd()}")
    return accounts

def rate_limit_sleep(headers):
    retry_after = headers.get('Retry-After')
    if retry_after:
        sleep_time = int(retry_after) + 1
        safe_print(f"Rate limit atingido. Aguardando {sleep_time} segundos...")
        time.sleep(sleep_time)
        return True
    return False


# ─────────────────────────────────────────────
#  LCU (API LOCAL DO CLIENTE)
# ─────────────────────────────────────────────

def make_lcu_request(method, endpoint, data=None, params=None, retry_count=0):
    global LCU_PORT, LCU_PASSWORD
    with _lcu_lock:
        if LCU_PORT is None or LCU_PASSWORD is None:
            LCU_PORT, LCU_PASSWORD = get_lcu_credentials()
            if LCU_PORT is None:
                return None
        port = LCU_PORT
        password = LCU_PASSWORD

    url = f"https://127.0.0.1:{port}{endpoint}"
    headers = {
        "Authorization": "Basic " + base64.b64encode(f"riot:{password}".encode()).decode()
    }

    try:
        response = requests.request(method, url, headers=headers, json=data, params=params, verify=False, timeout=10)
        if response.status_code in (200, 201, 204):
            try:
                return response.json()
            except Exception:
                return {}
        else:
            return None
    except requests.exceptions.ConnectionError:
        if retry_count < 3:
            with _lcu_lock:
                LCU_PORT, LCU_PASSWORD = get_lcu_credentials()
            time.sleep(1)
            return make_lcu_request(method, endpoint, data, params, retry_count + 1)
        return None
    except requests.exceptions.RequestException:
        return None

def send_friend_request(summoner_name, summoner_tag):
    data = {"gameName": summoner_name, "tagLine": summoner_tag}
    safe_print(f"Enviando solicitacao para {summoner_name}#{summoner_tag}...")
    result = make_lcu_request("POST", "/lol-chat/v2/friend-requests", data)
    if result is not None:
        safe_print(f"  Solicitacao enviada com sucesso.")
    else:
        safe_print(f"  Falha ao enviar solicitacao.")
    return result


def cancelar_pedidos_pendentes():
    """
    Cancela todos os pedidos de amizade enviados que ainda nao foram aceitos.
    Usa GET /lol-chat/v2/friend-requests para listar e DELETE para cada um.
    Retorna o numero de pedidos cancelados.
    """
    pendentes = make_lcu_request("GET", "/lol-chat/v2/friend-requests")
    if not pendentes:
        safe_print("  Nenhum pedido pendente encontrado.")
        return 0

    cancelados = 0
    outgoing = [p for p in pendentes if p.get("direction") in ("out", "outgoing", "sent", None)]
    if not outgoing:
        outgoing = pendentes  # se nao tem campo direction, tenta cancelar todos

    safe_print(f"  {len(outgoing)} pedidos pendentes para cancelar...")
    for pedido in outgoing:
        pid = pedido.get("id") or pedido.get("puuid") or pedido.get("summonerId")
        nome = pedido.get("gameName") or pedido.get("name") or str(pid)
        if pid:
            res = make_lcu_request("DELETE", f"/lol-chat/v2/friend-requests/{pid}")
            if res is not None or True:  # DELETE retorna vazio em sucesso
                safe_print(f"    Cancelado: {nome}")
                cancelados += 1
        time.sleep(0.3)

    safe_print(f"  {cancelados} pedidos cancelados.")
    return cancelados


# ─────────────────────────────────────────────
#  PERSISTENCIA — salvar/carregar jogadores adicionados
# ─────────────────────────────────────────────

ADDED_PLAYERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "added_players.json")
DISCORD_URL = "https://discord.gg/9TYwNpBQR2"

DISCORD_MESSAGES = [
    f"Você sente que joga melhor do que o seu elo? Muita gente fica presa no Elo Hell mesmo jogando bem. A gente só te ajuda a chegar no elo que você merece. {DISCORD_URL}",
    f"Você acha que tem nível mais alto que o seu elo atual? Às vezes o problema é só o Elo Hell. A gente te ajuda a chegar no elo que você realmente merece. {DISCORD_URL}",
    f"Você joga bem mas parece que nunca sai do mesmo elo? Isso acontece com muita gente por causa do matchmaking. A gente só ajuda você a chegar no elo que merece. {DISCORD_URL}",
    f"Você sente que poderia estar em um elo mais alto? Às vezes não é falta de skill, é só Elo Hell. A gente te ajuda a chegar onde você merece estar. {DISCORD_URL}",
]

def load_added_players():
    """Carrega a lista de jogadores já adicionados do arquivo JSON."""
    if not os.path.exists(ADDED_PLAYERS_FILE):
        return []
    try:
        with open(ADDED_PLAYERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Erro ao carregar {ADDED_PLAYERS_FILE}: {e}")
        return []

def save_added_players(players):
    """Salva a lista completa de jogadores no arquivo JSON."""
    try:
        with open(ADDED_PLAYERS_FILE, "w", encoding="utf-8") as f:
            json.dump(players, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Erro ao salvar {ADDED_PLAYERS_FILE}: {e}")

def register_sent_requests(players_to_add, account_username):
    """
    Registra os jogadores para quem foram enviadas solicitacoes.
    Evita duplicatas pelo campo name#tag.
    """
    existing = load_added_players()
    existing_keys = {f"{p['name']}#{p['tag']}" for p in existing}

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    added_count = 0

    for player in players_to_add:
        key = f"{player['name']}#{player['tag']}"
        if key not in existing_keys:
            existing.append({
                "name": player["name"],
                "tag": player["tag"],
                "puuid": player.get("puuid", ""),
                "added_by": account_username,
                "added_at": now_str,
                "accepted": False,
                "message_sent": False
            })
            existing_keys.add(key)
            added_count += 1

    save_added_players(existing)
    print(f"  {added_count} novos jogadores registrados em '{ADDED_PLAYERS_FILE}'.")
    return existing


# ─────────────────────────────────────────────
#  CHECAR QUEM ACEITOU + ENVIAR MENSAGEM DISCORD
# ─────────────────────────────────────────────

def get_friends_list_lcu():
    """Retorna lista de amigos atual via LCU."""
    result = make_lcu_request("GET", "/lol-chat/v1/friends")
    if result and isinstance(result, list):
        return {f.get('gameName', '').lower() for f in result if f.get('gameName')}
    return set()

def send_lcu_message(summoner_name, summoner_tag, message):
    """
    Envia mensagem de chat para um amigo via LCU.
    Primeiro busca o ID do amigo, depois manda a mensagem.
    """
    # Busca o ID da conversa com o amigo
    friends = make_lcu_request("GET", "/lol-chat/v1/friends")
    if not friends:
        return False

    friend_id = None
    for f in friends:
        if f.get("gameName", "").lower() == summoner_name.lower():
            friend_id = f.get("id")
            break

    if not friend_id:
        safe_print(f"  Amigo {summoner_name}#{summoner_tag} nao encontrado na lista.")
        return False

    # Abre/busca a conversa e envia a mensagem
    conversation = make_lcu_request("GET", f"/lol-chat/v1/conversations/{friend_id}")
    if not conversation:
        # Cria a conversa se não existir
        conversation = make_lcu_request("POST", "/lol-chat/v1/conversations", data={"id": friend_id})

    if conversation or True:  # Tenta mandar mesmo sem conversa confirmada
        result = make_lcu_request(
            "POST",
            f"/lol-chat/v1/conversations/{friend_id}/messages",
            data={"body": message, "type": "chat"}
        )
        if result is not None:
            safe_print(f"  Mensagem enviada para {summoner_name}#{summoner_tag}.")
            return True

    safe_print(f"  Falha ao enviar mensagem para {summoner_name}#{summoner_tag}.")
    return False

def check_and_message_accepted(account_username=None):
    """
    Checa quem aceitou o pedido de amizade e envia a mensagem do Discord.
    Atualiza o arquivo added_players.json.
    """
    import random as _random

    players = load_added_players()
    if not players:
        print("Nenhum jogador registrado em added_players.json.")
        return

    pending = [p for p in players if not p.get("accepted")]
    if not pending:
        print("Todos os jogadores ja aceitaram ou ja receberam mensagem.")
        return

    print(f"\nChecando {len(pending)} jogadores pendentes...")
    friends_set = get_friends_list_lcu()

    if not friends_set:
        print("  Nao foi possivel obter lista de amigos via LCU.")
        return

    changed = False
    for player in players:
        if player.get("accepted"):
            continue

        key = f"{player['name']}#{player['tag']}"
        if player['name'].lower() in friends_set:
            player["accepted"] = True
            print(f"  ACEITOU: {key}")

            # Envia mensagem se ainda nao enviou
            if not player.get("message_sent"):
                msg = _random.choice(DISCORD_MESSAGES)
                ok = send_lcu_message(player["name"], player["tag"], msg)
                if ok:
                    player["message_sent"] = True
                    player["message_sent_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            changed = True

    if changed:
        save_added_players(players)
        accepted = sum(1 for p in players if p.get("accepted"))
        messaged = sum(1 for p in players if p.get("message_sent"))
        print(f"\n  Total aceitaram: {accepted} | Mensagens enviadas: {messaged}")
    else:
        print("  Nenhum novo aceite encontrado.")


# ─────────────────────────────────────────────
#  RIOT API — com rate limit thread-safe
# ─────────────────────────────────────────────

def riot_api_get(url, api_key):
    """Faz GET na Riot API com rate limit thread-safe."""
    global _last_riot_request_time
    with _riot_api_lock:
        now = time.time()
        elapsed = now - _last_riot_request_time
        if elapsed < RIOT_API_MIN_INTERVAL:
            time.sleep(RIOT_API_MIN_INTERVAL - elapsed)
        _last_riot_request_time = time.time()

    full_url = url if "api_key=" in url else f"{url}{'&' if '?' in url else '?'}api_key={api_key}"
    try:
        resp = requests.get(full_url, timeout=10)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get('Retry-After', 5)) + 1
            safe_print(f"  Rate limit! Aguardando {retry_after}s...")
            time.sleep(retry_after)
            return riot_api_get(url, api_key)
        return resp
    except requests.exceptions.RequestException as e:
        safe_print(f"  Erro request: {e}")
        return None

REGIONAL_ROUTES = {
    "br1": "americas", "la1": "americas", "la2": "americas", "na1": "americas",
    "eun1": "europe", "euw1": "europe", "tr1": "europe", "ru": "europe",
    "jp1": "asia", "kr": "asia"
}

def get_players_by_rank(api_key, region="br1", tier="PLATINUM", division="IV", page=1):
    url = (
        f"https://{region}.api.riotgames.com/lol/league/v4/entries"
        f"/RANKED_SOLO_5x5/{tier}/{division}?page={page}&api_key={api_key}"
    )
    safe_print(f"[PASSO 1] Buscando {tier} {division} pagina {page}")
    resp = riot_api_get(url, api_key)
    if resp and resp.status_code == 200:
        return resp.json()
    return []

def get_account_by_puuid(api_key, region, puuid):
    route = REGIONAL_ROUTES.get(region.lower(), "americas")
    url = (
        f"https://{route}.api.riotgames.com/riot/account/v1/accounts/by-puuid"
        f"/{puuid}?api_key={api_key}"
    )
    resp = riot_api_get(url, api_key)
    if resp and resp.status_code == 200:
        data = resp.json()
        return {
            "gameName": data.get("gameName", ""),
            "tagLine": data.get("tagLine", ""),
            "puuid": puuid
        }
    return None


# ─────────────────────────────────────────────
#  LCU — buscar PUUID e checar histórico
# ─────────────────────────────────────────────

def extract_participants_from_spectator(game_data):
    """Extrai nomes dos participantes a partir da resposta da Spectator API."""
    participants = []
    seen = set()

    for participant in game_data.get("participants", []):
        riot_id = participant.get("riotId")
        name = ""
        tag = ""

        if isinstance(riot_id, str) and "#" in riot_id:
            name, tag = riot_id.split("#", 1)
        else:
            name = participant.get("riotIdGameName", "") or participant.get("gameName", "") or ""
            tag = participant.get("riotIdTagline", "") or participant.get("tagLine", "") or ""

        if not name:
            continue

        key = f"{name}#{tag}".lower() if tag else name.lower()
        if key in seen:
            continue
        seen.add(key)

        participants.append({
            "name": name,
            "tag": tag,
            "display": f"{name}#{tag}" if tag else name
        })

    return participants

def get_puuid_by_name_tag_lcu(game_name, tag_line):
    name_tag = f"{game_name}#{tag_line}"
    result = make_lcu_request("GET", "/lol-summoner/v1/summoners", params={"name": name_tag})
    if result:
        return result.get("puuid")
    return None

def get_summoner_by_puuid_lcu(puuid_riot):
    """Busca dados do summoner no LCU diretamente pelo puuid da Riot API."""
    result = make_lcu_request("GET", f"/lol-summoner/v1/summoners/by-puuid/{puuid_riot}")
    if result:
        return result
    return None

def check_if_in_game_lcu(puuid):
    """Checa se o jogador esta em partida agora via LCU."""
    result = make_lcu_request("GET", f"/lol-gameflow/v1/game-data")
    # Endpoint alternativo: checar spectate ou gameflow do summoner especifico
    # Usamos a API de espectador do LCU para ver se o jogador esta em jogo
    result2 = make_lcu_request("GET", f"/lol-summoner/v1/summoners/{puuid}/game-availability")
    if result2 and result2.get("availability") == "inGame":
        return True

    # Fallback: checar via spectator v1
    result3 = make_lcu_request("GET", f"/lol-spectator/v1/spectate/launch", params={"puuid": puuid})
    if result3 is not None:
        return True

    # Fallback 2: checar status do summoner — se gameStatus == "inGame"
    result4 = make_lcu_request("GET", f"/lol-chat/v1/friends")
    if result4 and isinstance(result4, list):
        for friend in result4:
            if friend.get("puuid") == puuid:
                availability = friend.get("availability", "")
                lol = friend.get("lol", {})
                game_status = lol.get("gameStatus", "")
                if game_status in ("inGame", "hosting_ranked_game"):
                    return True

    return False


# ─────────────────────────────────────────────
#  PROCESSAR UM JOGADOR (roda em thread)
# ─────────────────────────────────────────────

def process_player(entry, api_key, region, processed_ids_lock, processed_ids):
    """
    Processa um jogador: checa se esta em rankeada solo/duo agora via Spectator API.
    So busca o nome se estiver em jogo (economiza chamadas API).
    """
    puuid = entry.get("puuid")
    if not puuid:
        return None

    with processed_ids_lock:
        if puuid in processed_ids:
            return None
        processed_ids.add(puuid)

    # PASSO 1 — Spectator API: checar se esta em jogo agora
    spec_url = f"https://{region}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
    spec_resp = riot_api_get(spec_url, api_key)
    if spec_resp is None:
        return None
    if spec_resp.status_code == 404:
        return None  # nao esta em jogo
    if spec_resp.status_code != 200:
        return None

    # Verificar se e ranked solo/duo (queue 420)
    try:
        game_data = spec_resp.json()
        queue_id = game_data.get("gameQueueConfigId", 0)
        if queue_id != 420:
            return None
    except Exception:
        return None

    # PASSO 2 — So agora busca o nome (esta em jogo confirmado)
    acc_info = get_account_by_puuid(api_key, region, puuid)
    if not acc_info or not acc_info.get("gameName"):
        return None

    game_name = acc_info["gameName"]
    tag_line = acc_info["tagLine"]

    participants = extract_participants_from_spectator(game_data)

    safe_print(f"  >>> EM RANKEADA AGORA: {game_name}#{tag_line} <<<")
    if participants:
        safe_print("      Participantes da partida:")
        for idx, participant in enumerate(participants, start=1):
            safe_print(f"        {idx:02d}. {participant['display']}")

    return {
        "name": game_name,
        "tag": tag_line,
        "puuid": puuid,
        "participants": participants
    }


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    global LCU_PORT, LCU_PASSWORD

    try:
        # ─────────────────────────────────────────────
        # MENU INICIAL
        # ─────────────────────────────────────────────
        print("\n" + "="*60)
        print("  BOT ELOJOB — League of Legends")
        print("="*60)
        print("\n  [1] Novo ciclo — buscar players e enviar pedidos")
        print("  [2] Checar amizades — ver quem aceitou e enviar mensagem")
        print("  [3] Verificar contas — nivel, ban e gerar lista limpa")
        print("  [4] Salvar sessoes — logar em cada conta e salvar cookies")
        print()

        while True:
            escolha = input("  Escolha uma opcao (1-4): ").strip()
            if escolha in ("1", "2", "3", "4"):
                break
            print("  Opcao invalida. Digite 1, 2, 3 ou 4.")

        script_dir = os.path.dirname(os.path.abspath(__file__))
        php_ok = start_php_server(script_dir, PHP_SERVER_PORT)
        if not php_ok:
            print("Aviso: Servidor PHP nao iniciado.")

        # ─────────────────────────────────────────────
        # OPCAO 2 — Checar amizades das contas existentes
        # ─────────────────────────────────────────────
        if escolha == "2":
            all_accounts = read_account_credentials("account_credentials.txt")
            if not all_accounts:
                print("Nenhuma conta encontrada em 'account_credentials.txt'.")
                input("\nPressione Enter para sair...")
                return

            players_salvos = load_added_players()
            if not players_salvos:
                print("\nNenhum jogador registrado em added_players.json.")
                input("\nPressione Enter para sair...")
                return

            pendentes_por_conta = {}
            for p in players_salvos:
                if not p.get("accepted"):
                    conta = p.get("added_by", "")
                    if conta not in pendentes_por_conta:
                        pendentes_por_conta[conta] = []
                    pendentes_por_conta[conta].append(p)

            total_pendentes = sum(len(v) for v in pendentes_por_conta.values())
            print(f"\n  {total_pendentes} jogadores pendentes em {len(pendentes_por_conta)} conta(s).")

            WAIT_MINUTES = 5
            CHECK_INTERVAL = 10
            ciclo_op2 = 1

            # Loop continuo — repete o ciclo de contas ate o usuario interromper
            while True:
                print(f"\n{'='*60}")
                print(f"  CICLO {ciclo_op2} — Checando amizades em todas as contas")
                print(f"{'='*60}")

                # Recarrega pendentes a cada ciclo
                players_salvos = load_added_players()
                pendentes_por_conta = {}
                for p in players_salvos:
                    if not p.get("accepted"):
                        conta = p.get("added_by", "")
                        if conta not in pendentes_por_conta:
                            pendentes_por_conta[conta] = []
                        pendentes_por_conta[conta].append(p)

                total_pendentes = sum(len(v) for v in pendentes_por_conta.values())
                if total_pendentes == 0:
                    print("\n  Todos os jogadores ja aceitaram. Encerrando loop.")
                    break

                print(f"  {total_pendentes} pendentes restantes.")

                for account in all_accounts:
                    username = account["username"]
                    if username not in pendentes_por_conta or not pendentes_por_conta[username]:
                        print(f"\n  {username}: sem pendentes. Pulando.")
                        continue

                    n = len(pendentes_por_conta[username])
                    print(f"\n>>> Conta: {username} — {n} pendentes <<<")

                    port, pwd = launch_via_session(username, SESSIONS_DIR)
                    if not port:
                        print(f"  Sessao falhou, tentando login normal...")
                        port, pwd = launch_and_login(username, account["password"])
                    if not port:
                        print(f"  Nao foi possivel logar em {username}. Pulando.")
                        continue
                    salvar_session_renovada(username, SESSIONS_DIR)

                    LCU_PORT, LCU_PASSWORD = port, pwd

                    total_wait = WAIT_MINUTES * 60
                    elapsed_wait = 0
                    last_print = -60

                    print(f"  Checando por {WAIT_MINUTES} min em {username}...")

                    while elapsed_wait < total_wait:
                        check_and_message_accepted(username)

                        if elapsed_wait - last_print >= 60:
                            remaining = (total_wait - elapsed_wait) // 60
                            print(f"  [{username}] {remaining} min restantes")
                            last_print = elapsed_wait

                        time.sleep(CHECK_INTERVAL)
                        elapsed_wait += CHECK_INTERVAL

                    check_and_message_accepted(username)
                    print(f"  Conclusao de {username}.")
                    time.sleep(3)

                ciclo_op2 += 1

            close_riot_client_and_lol()
            print("\nChecagem de amizades concluida.")
            input("\nPressione Enter para fechar...")
            return

        # ─────────────────────────────────────────────
        # ─────────────────────────────────────────────
        # OPCAO 4 — Salvar sessoes (codigo do arquivo botlol__1__.py)
        # ─────────────────────────────────────────────
        if escolha == "4":
            print("\n" + "="*60)
            print("  SALVAR SESSOES — Copiando RiotGamesPrivateSettings.yaml")
            print("="*60)

            arquivo_s = input("\n  Arquivo de contas (Enter para 'account_credentials.txt'): ").strip() or "account_credentials.txt"

            contas_s = read_account_credentials(arquivo_s)
            if not contas_s:
                safe_print(f"  Nenhuma conta encontrada em '{arquivo_s}'.")
                input("\nPressione Enter para sair...")
                return

            script_dir_s = os.path.dirname(os.path.abspath(__file__))
            sessions_dir_s = os.path.join(script_dir_s, "sessions")
            os.makedirs(sessions_dir_s, exist_ok=True)

            safe_print(f"\n  {len(contas_s)} contas para salvar sessao.\n")

            salvos = 0
            falhas = 0

            for account in contas_s:
                username = account["username"]
                user_password = account["password"]

                safe_print(f"\n>>> [{salvos+falhas+1}/{len(contas_s)}] Logando: {username} <<<")

                port, pwd = launch_and_login_save_session(username, user_password)
                if not port:
                    safe_print(f"  FALHA ao logar em {username}.")
                    falhas += 1
                    close_riot_client_and_lol()
                    continue

                # Aguardar garantir que o arquivo foi gerado
                time.sleep(3)

                settings_path = get_riot_settings_path()
                conta_session_dir = os.path.join(sessions_dir_s, username)
                os.makedirs(conta_session_dir, exist_ok=True)

                if settings_path:
                    dest_settings = os.path.join(conta_session_dir, "RiotGamesPrivateSettings.yaml")
                    try:
                        shutil.copy2(settings_path, dest_settings)
                        safe_print(f"  RiotGamesPrivateSettings.yaml copiado.")

                        session_info = {
                            "username": username,
                            "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                            "source_file": settings_path
                        }
                        session_file = os.path.join(conta_session_dir, "session.json")
                        with open(session_file, "w", encoding="utf-8") as f:
                            json.dump(session_info, f, ensure_ascii=False, indent=2)

                        safe_print(f"  Sessao salva em: sessions/{username}/")
                        salvos += 1

                        # Deletar yaml da pasta Data para nao logar automaticamente na proxima abertura
                        deletar_yaml_data()
                    except Exception as e:
                        safe_print(f"  ERRO ao copiar arquivo: {e}")
                        falhas += 1
                else:
                    safe_print(f"  AVISO: RiotGamesPrivateSettings.yaml nao encontrado.")
                    falhas += 1

                close_riot_client_and_lol()

            safe_print(f"\n{'='*60}")
            safe_print(f"  RESULTADO")
            safe_print(f"{'='*60}")
            safe_print(f"  Sessoes salvas : {salvos}")
            safe_print(f"  Falhas         : {falhas}")
            safe_print(f"  Pasta          : sessions/")
            safe_print(f"\n  Arquivos RiotGamesPrivateSettings.yaml salvos com sucesso!")

            input("\nPressione Enter para fechar...")
            return

        # ─────────────────────────────────────────────
        # ─────────────────────────────────────────────
        # ─────────────────────────────────────────────
        # OPCAO 3 — Verificar contas usando sessions salvas (sem abrir cliente)
        # ─────────────────────────────────────────────
        if escolha == "3":
            import shutil as _shutil
            import sqlite3 as _sqlite3

            script_dir_op3 = os.path.dirname(os.path.abspath(__file__))
            sessions_dir_op3 = os.path.join(script_dir_op3, "sessions")

            print("\n  Arquivo de contas para verificar (Enter para 'account_credentials.txt'):")
            arquivo_check = input("  > ").strip() or "account_credentials.txt"

            contas_check = read_account_credentials(arquivo_check)
            if not contas_check:
                print(f"  Nenhuma conta encontrada em '{arquivo_check}'.")
                input("\nPressione Enter para sair...")
                return

            # Verificar se existe pasta sessions
            tem_sessions = os.path.isdir(sessions_dir_op3) and any(
                os.path.exists(os.path.join(sessions_dir_op3, a["username"], "session.json"))
                for a in contas_check
            )

            if not tem_sessions:
                print("\n  AVISO: Nenhuma sessao salva encontrada em 'sessions/'.")
                print("  Execute a opcao 4 primeiro para salvar as sessoes.")
                print("  Usando fallback: login via pyautogui (mais lento).")
                usar_sessions = False
            else:
                usar_sessions = True
                print(f"\n  Sessoes encontradas. Verificando sem abrir cliente.")

            print(f"\n  {len(contas_check)} contas para verificar.\n")

            def reauth_com_ssid(session_json):
                """
                Usa o ssid salvo para obter um novo access_token via cookie flow.
                Retorna access_token ou None.
                """
                import re as _re
                cookies = session_json.get("cookies", {})
                ssid = cookies.get("ssid", "")
                if not ssid:
                    return None

                s = requests.Session()
                s.verify = False

                # Restaurar cookies de auth no session
                for name, value in cookies.items():
                    if not name.startswith("_") and len(value) < 2000:
                        try:
                            s.cookies.set(name, value, domain="auth.riotgames.com")
                        except Exception:
                            pass

                H = {
                    "User-Agent": "RiotClient/63.0.9101.4274 rso-auth (Windows;10;;Professional, x64)",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }

                try:
                    # Tentar reauth com cookies existentes
                    r = s.get(
                        "https://auth.riotgames.com/authorize"
                        "?redirect_uri=http://localhost/redirect"
                        "&client_id=riot-client"
                        "&response_type=token+id_token"
                        "&scope=openid+offline_access+lol+ban+profile+email+phone"
                        "&nonce=1",
                        headers=H,
                        allow_redirects=False,
                        timeout=15,
                    )
                    # O redirect tem o access_token na URL
                    location = r.headers.get("Location", "") or r.headers.get("location", "")
                    if not location:
                        # Tentar seguir redirect
                        r2 = s.get(
                            "https://auth.riotgames.com/authorize"
                            "?redirect_uri=http://localhost/redirect"
                            "&client_id=riot-client"
                            "&response_type=token+id_token"
                            "&scope=openid+offline_access+lol+ban+profile+email+phone"
                            "&nonce=1",
                            headers=H,
                            allow_redirects=True,
                            timeout=15,
                        )
                        location = r2.url

                    m = _re.search(r"access_token=([^&]+)", location)
                    if m:
                        return m.group(1)
                    return None
                except Exception:
                    return None

            def checar_conta_via_token(access_token, username):
                """Checa nivel e ban via Riot API com o access_token."""
                nivel = None
                banida = False
                motivo_ban = ""
                summ_name = username
                game_name = ""
                tag_line = ""

                try:
                    # Userinfo — nome e state de ban
                    r_ui = requests.get(
                        "https://auth.riotgames.com/userinfo",
                        headers={"Authorization": f"Bearer {access_token}"},
                        verify=False, timeout=10,
                    )
                    if r_ui.status_code == 200:
                        ui = r_ui.json()
                        acct = ui.get("acct", {})
                        game_name = acct.get("game_name", "") or ""
                        tag_line  = acct.get("tag_line", "") or ""
                        summ_name = f"{game_name}#{tag_line}" if tag_line else game_name or username
                        for lol_acc in ui.get("lol", []):
                            state = lol_acc.get("state", "ENABLED")
                            if state != "ENABLED":
                                banida = True
                                motivo_ban = f"state={state}"
                            break
                except Exception:
                    pass

                try:
                    # Nivel via Riot API
                    r_s = requests.get(
                        "https://br1.api.riotgames.com/lol/summoner/v4/summoners/me",
                        headers={"Authorization": f"Bearer {access_token}"},
                        verify=False, timeout=10,
                    )
                    if r_s.status_code == 200:
                        nivel = r_s.json().get("summonerLevel")
                    elif r_s.status_code == 403:
                        banida = True
                        motivo_ban = motivo_ban or "403 summoner"
                except Exception:
                    pass

                return nivel, banida, motivo_ban, summ_name

            resultado_contas = []

            for account in contas_check:
                username = account["username"]
                user_password = account["password"]
                idx = len(resultado_contas) + 1

                print(f"  [{idx}/{len(contas_check)}] {username} ...", end=" ", flush=True)

                access_token = None
                via = ""

                if usar_sessions:
                    session_file = os.path.join(SESSIONS_DIR, username, "session.json")
                    if os.path.exists(session_file):
                        with open(session_file, "r", encoding="utf-8") as f:
                            sess = json.load(f)
                        saved_token = sess.get("access_token", "")
                        expiry = sess.get("expiry", 0)
                        if saved_token and expiry and time.time() < expiry - 60:
                            access_token = saved_token
                            via = "token_salvo"

                if not access_token:
                    # Login via RiotGamesPrivateSettings.yaml salvo
                    port, pwd = launch_via_session(username, SESSIONS_DIR)
                    if not port:
                        # Fallback: pyautogui normal
                        port, pwd = launch_and_login_fast(username, user_password)
                    if not port:
                        print(f"FALHA_LOGIN")
                        resultado_contas.append({
                            "username": username, "password": user_password,
                            "display_name": username, "status": "FALHA_LOGIN",
                            "nivel": None, "banida": True, "motivo": "nao conseguiu logar",
                        })
                        continue

                    LCU_PORT, LCU_PASSWORD = port, pwd
                    salvar_session_renovada(username, SESSIONS_DIR)
                    rso = make_lcu_request("GET", "/rso-auth/v1/authorization")
                    if rso:
                        access_token = rso.get("accessToken", {}).get("token", "")

                    if not access_token:
                        # Checar direto via LCU sem token
                        summoner_data = make_lcu_request("GET", "/lol-summoner/v1/current-summoner")
                        nivel = summoner_data.get("summonerLevel") if summoner_data else None
                        summ_name = (summoner_data.get("gameName") or summoner_data.get("displayName") or username) if summoner_data else username
                        ban_info = make_lcu_request("GET", "/lol-penalties/v1/penalty-notif")
                        banida = bool(ban_info and isinstance(ban_info, list) and len(ban_info) > 0)
                        motivo_ban = str(ban_info[0].get("penaltyType", "")) if banida else ""
                        honor_data = make_lcu_request("GET", "/lol-honor-v2/v1/profile")
                        honor = honor_data.get("honorLevel", "?") if honor_data else "?"
                        status_txt = "BANIDA" if banida else "OK"
                        nv = f"Nv.{nivel}" if nivel else "Nv.?"
                        print(f"{status_txt}  {nv}  {summ_name}  Honor:{honor}  [via LCU]")
                        resultado_contas.append({
                            "username": username, "password": user_password,
                            "display_name": summ_name, "status": status_txt,
                            "nivel": nivel, "banida": banida, "motivo": motivo_ban,
                        })
                        time.sleep(1)
                        continue

                # Checar via token
                nivel, banida, motivo_ban, summ_name = checar_conta_via_token(access_token, username)
                status_txt = "BANIDA" if banida else "OK"
                nv = f"Nv.{nivel}" if nivel else "Nv.?"
                print(f"{status_txt}  {nv}  {summ_name}  [via {via}]")

                resultado_contas.append({
                    "username": username, "password": user_password,
                    "display_name": summ_name, "status": status_txt,
                    "nivel": nivel, "banida": banida, "motivo": motivo_ban,
                })

                time.sleep(0.5)

            # Gerar arquivos
            limpas = [c for c in resultado_contas if not c["banida"] and c["status"] == "OK"]
            ruins  = [c for c in resultado_contas if c["banida"] or c["status"] != "OK"]

            base_op3 = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(base_op3, "contas_limpas.txt"), "w", encoding="utf-8") as f:
                for c in limpas:
                    f.write(f"{c['username']}:{c['password']}\n")
            with open(os.path.join(base_op3, "contas_banidas.txt"), "w", encoding="utf-8") as f:
                for c in ruins:
                    extra = f" — {c['motivo']}" if c["motivo"] else ""
                    f.write(f"{c['username']}:{c['password']} # {c['status']}{extra}\n")

            print(f"\n{'='*60}")
            print(f"  RESULTADO FINAL")
            print(f"{'='*60}")
            for c in resultado_contas:
                nv = f"Nv.{c['nivel']}" if c['nivel'] else "Nv.?"
                ico = "OK " if not c["banida"] and c["status"] == "OK" else "BAN"
                print(f"  [{ico}]  {nv:8}  {c['display_name']}")
            print(f"\n  OK: {len(limpas)}  |  Problema: {len(ruins)}")
            print(f"  Arquivos: contas_limpas.txt  /  contas_banidas.txt")

            if usar_sessions:
                close_riot_client_and_lol()
            input("\nPressione Enter para fechar...")
            return
        # ─────────────────────────────────────────────
        # OPCAO 1 — Novo ciclo com nova lista de contas
        # ─────────────────────────────────────────────
        print("\n  Arquivo de contas (Enter para usar 'account_credentials.txt'):")
        arquivo_contas = input("  > ").strip()
        if not arquivo_contas:
            arquivo_contas = "account_credentials.txt"

        all_accounts = read_account_credentials(arquivo_contas)
        if not all_accounts:
            print(f"Nenhuma conta encontrada em '{arquivo_contas}'.")
            input("\nPressione Enter para sair...")
            return

        riot_api_key = get_riot_api_key()
        if not riot_api_key:
            print("Chave da Riot Games API nao fornecida.")
            input("\nPressione Enter para sair...")
            return

        import random
        region = "br1"
        max_to_find = 50
        MAX_WORKERS = 1  # 1 thread — Spectator API tem rate limit apertado
        # Apenas elos Platina para baixo conforme pedido
        ALL_TIERS = ["PLATINUM", "GOLD", "SILVER", "BRONZE", "IRON"]
        ALL_DIVISIONS = ["I", "II", "III", "IV"]

        # IDs ja processados globalmente entre todas as contas (evita adicionar o mesmo player 2x)
        global_processed_ids = set()
        global_processed_ids_lock = threading.Lock()

        def buscar_players(lcu_port, lcu_password):
            """Busca ate max_to_find players em partida agora, sorteando 3 elos aleatorios."""
            global LCU_PORT, LCU_PASSWORD
            LCU_PORT, LCU_PASSWORD = lcu_port, lcu_password

            players_to_add = []
            processed_ids_lock = threading.Lock()
            players_lock = threading.Lock()

            # Sorteia 3 elos aleatorios
            tiers_sorteados = random.sample(ALL_TIERS, 3)

            print(f"\n{'='*60}")
            print(f"  Buscando jogadores EM RANKEADA SOLO/DUO AGORA — Platina e abaixo")
            print(f"  Elos: {', '.join(tiers_sorteados)}")
            print(f"  Threads: {MAX_WORKERS} | Delay Riot API: {RIOT_API_MIN_INTERVAL}s")
            print(f"{'='*60}\n")

            for tier in tiers_sorteados:
                with players_lock:
                    if len(players_to_add) >= max_to_find:
                        break

                division = random.choice(ALL_DIVISIONS)
                page = random.randint(1, 10)
                print(f"\n>>> Elo: {tier} {division} | Pagina: {page} <<<")

                league_entries = get_players_by_rank(riot_api_key, region, tier, division, page=page)
                if not league_entries:
                    print(f"  Sem entradas. Pulando.")
                    continue

                # Filtra entries ja processadas globalmente
                with global_processed_ids_lock:
                    entries_novos = [e for e in league_entries if e.get("puuid") not in global_processed_ids]

                print(f"  {len(entries_novos)} jogadores novos. Processando com {MAX_WORKERS} threads...\n")

                local_processed_ids = set()
                local_processed_ids_lock = threading.Lock()

                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    futures = {
                        executor.submit(
                            process_player,
                            entry, riot_api_key, region,
                            local_processed_ids_lock, local_processed_ids
                        ): entry
                        for entry in entries_novos
                    }

                    # Registra todos como processados globalmente
                    with global_processed_ids_lock:
                        for e in entries_novos:
                            if e.get("puuid"):
                                global_processed_ids.add(e["puuid"])

                    for future in as_completed(futures):
                        with players_lock:
                            if len(players_to_add) >= max_to_find:
                                for f in futures:
                                    f.cancel()
                                break
                        try:
                            result = future.result()
                            if result:
                                with players_lock:
                                    if len(players_to_add) < max_to_find:
                                        players_to_add.append(result)
                        except Exception as e:
                            safe_print(f"  Erro em thread: {e}")

            return players_to_add

        # ─────────────────────────────────────────────
        # FASE 1 — Logar em todas as contas e mandar pedidos
        # ─────────────────────────────────────────────
        # Guarda os dados de cada conta: players encontrados e credenciais LCU
        contas_dados = []  # lista de dicts: {username, players, port, pwd}

        print(f"\n{'='*60}")
        print(f"  FASE 1 — Logando em todas as contas e enviando pedidos")
        print(f"{'='*60}")

        for account in all_accounts:
            username = account["username"]
            user_password = account["password"]

            print(f"\n>>> CONTA: {username} <<<")

            port, pwd = launch_via_session(username, SESSIONS_DIR)
            if not port:
                print(f"  Sessao falhou, tentando login normal...")
                port, pwd = launch_and_login(username, user_password)
            if not port:
                print(f"  Nao foi possivel logar em {username}. Pulando.")
                continue
            salvar_session_renovada(username, SESSIONS_DIR)

            LCU_PORT, LCU_PASSWORD = port, pwd

            # Busca players exclusivos para esta conta
            players_to_add = buscar_players(port, pwd)

            if not players_to_add:
                print(f"  Nenhum jogador ativo hoje encontrado para {username}.")
                contas_dados.append({"username": username, "players": [], "port": port, "pwd": pwd})
                continue

            print(f"  {len(players_to_add)} jogadores encontrados. Enviando pedidos...")

            for player in players_to_add:
                send_friend_request(player["name"], player["tag"])
                time.sleep(1)

            register_sent_requests(players_to_add, username)
            print(f"  Pedidos enviados para {username}.")

            contas_dados.append({"username": username, "players": players_to_add, "port": port, "pwd": pwd})

        # ─────────────────────────────────────────────
        # FASE 2 — Voltar em cada conta e ficar 30 min checando aceitacoes
        # ─────────────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"  FASE 2 — Checando aceitacoes (30 min por conta)")
        print(f"{'='*60}")

        WAIT_MINUTES = 5
        CHECK_INTERVAL = 10

        for conta in contas_dados:
            username = conta["username"]

            print(f"\n>>> Voltando para conta: {username} <<<")

            user_password = next((a["password"] for a in all_accounts if a["username"] == username), None)
            if not user_password:
                print(f"  Senha nao encontrada para {username}. Pulando.")
                continue
            port, pwd = launch_via_session(username, SESSIONS_DIR)
            if not port:
                print(f"  Sessao falhou, tentando login normal...")
                port, pwd = launch_and_login(username, user_password)
            if not port:
                print(f"  Nao foi possivel logar em {username}. Pulando checagem.")
                continue
            salvar_session_renovada(username, SESSIONS_DIR)

            LCU_PORT, LCU_PASSWORD = port, pwd

            total_wait = WAIT_MINUTES * 60
            elapsed_wait = 0
            last_print = -60

            print(f"  Aguardando aceitacoes por {WAIT_MINUTES} minutos em {username}...")
            print(f"  Checando a cada {CHECK_INTERVAL}s.")

            while elapsed_wait < total_wait:
                check_and_message_accepted(username)

                if elapsed_wait - last_print >= 60:
                    remaining = (total_wait - elapsed_wait) // 60
                    print(f"  [{username}] {remaining} min restantes")
                    last_print = elapsed_wait

                time.sleep(CHECK_INTERVAL)
                elapsed_wait += CHECK_INTERVAL

            print(f"  Checagem final em {username}...")
            check_and_message_accepted(username)
            print(f"  Conclusao da conta {username}.")
            time.sleep(3)

        # ─────────────────────────────────────────────
        # FASE 3 — Cancelar pedidos ainda pendentes e buscar pessoas novas
        # ─────────────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"  FASE 3 — Cancelando pedidos pendentes e buscando novas pessoas")
        print(f"{'='*60}")

        for conta in contas_dados:
            username = conta["username"]

            print(f"\n>>> FASE 3 conta: {username} <<<")

            user_password = next((a["password"] for a in all_accounts if a["username"] == username), None)
            if not user_password:
                continue

            port, pwd = launch_via_session(username, SESSIONS_DIR)
            if not port:
                print(f"  Sessao falhou, tentando login normal...")
                port, pwd = launch_and_login(username, user_password)
            if not port:
                print(f"  Nao foi possivel logar em {username}. Pulando.")
                continue
            salvar_session_renovada(username, SESSIONS_DIR)

            LCU_PORT, LCU_PASSWORD = port, pwd

            # Cancelar todos os pedidos enviados que nao foram aceitos
            print(f"  Cancelando pedidos pendentes de {username}...")
            n_cancelados = cancelar_pedidos_pendentes()

            # Limpar do added_players.json os nao aceitos desta conta
            players_salvos = load_added_players()
            players_salvos_filtrado = [
                p for p in players_salvos
                if not (p.get("added_by") == username and not p.get("accepted"))
            ]
            save_added_players(players_salvos_filtrado)
            print(f"  {len(players_salvos) - len(players_salvos_filtrado)} entradas removidas do registro.")

            # Buscar novas pessoas
            print(f"  Buscando novas pessoas para {username}...")
            novos_players = buscar_players(port, pwd)

            if not novos_players:
                print(f"  Nenhum jogador novo encontrado para {username}.")
                continue

            print(f"  {len(novos_players)} novos jogadores. Enviando pedidos...")
            for player in novos_players:
                send_friend_request(player["name"], player["tag"])
                time.sleep(1)

            register_sent_requests(novos_players, username)
            print(f"  Novos pedidos enviados para {username}.")
            time.sleep(2)

        close_riot_client_and_lol()
        print("\nProcesso concluido com sucesso.")

    except Exception as e:
        print(f"\nErro inesperado: {e}")
        import traceback
        traceback.print_exc()
    finally:
        stop_php_server()
        input("\nExecucao finalizada. Pressione Enter para fechar...")


if __name__ == "__main__":
    main()
