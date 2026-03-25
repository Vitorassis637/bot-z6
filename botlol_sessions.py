import os
import sys
import time
import json
import base64
import shutil
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

try:
    import psutil
    import requests
    import urllib3
except ImportError:
    print("Erro: Bibliotecas necessárias não instaladas.")
    print("Execute: pip install psutil requests urllib3")
    sys.exit(1)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_print_lock = threading.Lock()

def safe_print(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)

LCU_PORT = None
LCU_PASSWORD = None
RIOT_CLIENT_PATHS = [
    r"C:\Riot Games\Riot Client\RiotClientServices.exe",
    r"C:\Program Files\Riot Games\Riot Client\RiotClientServices.exe",
    r"C:\Program Files (x86)\Riot Games\Riot Client\RiotClientServices.exe",
]

RIOT_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Riot Games", "Riot Client", "Data"
)
RIOT_SETTINGS_FILENAME = "RiotGamesPrivateSettings.yaml"

SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")

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

def close_riot_client_and_lol():
    safe_print("Fechando League of Legends e Riot Client...")
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] in ["LeagueClient.exe", "RiotClientServices.exe", "League of Legends.exe"]:
            try:
                proc.kill()
                safe_print(f"Processo {proc.info['name']} encerrado.")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    time.sleep(5)

def find_league_client_exe():
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
        safe_print("Riot Client já está em execução.")
        return True
    exe = find_riot_client_exe()
    if not exe:
        safe_print("Erro: Não foi possível encontrar o RiotClientServices.exe.")
        return False
    safe_print(f"Abrindo Riot Client: {exe}")
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
        safe_print(f"Erro ao abrir o Riot Client: {e}")
        return False

def is_league_client_running():
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] == "LeagueClient.exe":
            return True
    return False

def wait_for_league_client(timeout=120, poll_interval=3):
    safe_print(f"Aguardando LeagueClient.exe iniciar (timeout: {timeout}s)...")
    elapsed = 0
    while elapsed < timeout:
        if is_league_client_running():
            safe_print(f"LeagueClient.exe detectado após {elapsed}s.")
            return True
        time.sleep(poll_interval)
        elapsed += poll_interval
    safe_print("Timeout: LeagueClient.exe não foi detectado.")
    return False

def wait_for_lcu_ready(timeout=90, poll_interval=3):
    safe_print(f"Aguardando LCU ficar disponível (timeout: {timeout}s)...")
    elapsed = 0
    while elapsed < timeout:
        try:
            lockfile_path = os.path.join(os.getenv("LOCALAPPDATA", ""), "Riot Games", "Riot Client", "Config", "lockfile")
            if os.path.exists(lockfile_path):
                with open(lockfile_path, "r") as f:
                    parts = f.read().strip().split(':')
                    if len(parts) >= 4:
                        port = parts[2]
                        password = parts[3]
                        url = f"https://127.0.0.1:{port}/lol-service-status/v1/lcu-status"
                        headers = {
                            "Authorization": "Basic " + base64.b64encode(f"riot:{password}".encode()).decode()
                        }
                        resp = requests.get(url, headers=headers, verify=False, timeout=3)
                        if resp.status_code in (200, 404):
                            safe_print(f"  LCU disponível na porta {port}.")
                            return port, password
        except Exception:
            pass
        time.sleep(poll_interval)
        elapsed += poll_interval
        safe_print(f"  Aguardando LCU... {elapsed}s/{timeout}s")
    safe_print("Timeout: LCU não ficou disponível.")
    return None, None

def type_with_clipboard(text):
    try:
        proc = subprocess.Popen(['clip'], stdin=subprocess.PIPE, close_fds=True)
        proc.communicate(input=text.encode('utf-16-le'))
    except Exception:
        pass

# ─────────────────────────────────────────────
#  GERENCIAMENTO DE SESSOES
# ─────────────────────────────────────────────

def clear_riot_client_data():
    """Limpa a pasta Data do Riot Client antes do login."""
    data_dir = RIOT_DATA_DIR
    if not os.path.exists(data_dir):
        safe_print(f"  AVISO: Pasta Data não encontrada: {data_dir}")
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
    """Retorna o caminho do RiotGamesPrivateSettings.yaml se existir."""
    base_path = os.path.join(RIOT_DATA_DIR, RIOT_SETTINGS_FILENAME)
    return base_path if os.path.exists(base_path) else None

def restaurar_session(username, sessions_dir):
    """Copia RiotGamesPrivateSettings.yaml de sessions/<username>/ para a pasta Data."""
    src = os.path.join(sessions_dir, username, RIOT_SETTINGS_FILENAME)
    dst = os.path.join(RIOT_DATA_DIR, RIOT_SETTINGS_FILENAME)

    safe_print(f"  [SESSION] Procurando: {src}")

    if not os.path.exists(src):
        safe_print(f"  [SESSION] Arquivo nao encontrado!")
        if os.path.isdir(sessions_dir):
            safe_print(f"  [SESSION] Contas em sessions/: {os.listdir(sessions_dir)}")
        else:
            safe_print(f"  [SESSION] Pasta sessions/ nao existe: {sessions_dir}")
        return False

    try:
        os.makedirs(RIOT_DATA_DIR, exist_ok=True)
        shutil.copy2(src, dst)
        safe_print(f"  Sessão restaurada para {username}.")
        return True
    except Exception as e:
        safe_print(f"  ERRO ao restaurar sessão: {e}")
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
    """Copia o yaml renovado de volta para sessions/<username>/ e deleta da Data."""
    settings_path = get_riot_settings_path()
    if not settings_path:
        safe_print(f"  AVISO: {RIOT_SETTINGS_FILENAME} não encontrado para salvar.")
        return False
    conta_dir = os.path.join(sessions_dir, username)
    try:
        os.makedirs(conta_dir, exist_ok=True)
        shutil.copy2(settings_path, os.path.join(conta_dir, RIOT_SETTINGS_FILENAME))
        session_info = {"username": username, "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}
        with open(os.path.join(conta_dir, "session.json"), "w", encoding="utf-8") as f:
            json.dump(session_info, f, ensure_ascii=False, indent=2)
        safe_print(f"  Sessão renovada salva para {username}.")
        deletar_yaml_data()
        return True
    except Exception as e:
        safe_print(f"  ERRO ao salvar sessão renovada: {e}")
        return False
    """Copia o RiotGamesPrivateSettings.yaml renovado de volta para sessions/<username>/."""
    settings_path = get_riot_settings_path()
    if not settings_path:
        safe_print(f"  AVISO: {RIOT_SETTINGS_FILENAME} não encontrado para salvar.")
        return False
    conta_dir = os.path.join(sessions_dir, username)
    try:
        os.makedirs(conta_dir, exist_ok=True)
        shutil.copy2(settings_path, os.path.join(conta_dir, RIOT_SETTINGS_FILENAME))
        # Atualizar session.json com timestamp
        session_file = os.path.join(conta_dir, "session.json")
        session_info = {"username": username, "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(session_info, f, ensure_ascii=False, indent=2)
        safe_print(f"  Sessão renovada salva para {username}.")
        return True
    except Exception as e:
        safe_print(f"  ERRO ao salvar sessão renovada: {e}")
        return False

def launch_via_session(username, sessions_dir):
    """
    Login via sessão salva — sem pyautogui, sem digitar senha.
    1. Fecha cliente (kill sem logout)
    2. Limpa pasta Data
    3. Restaura RiotGamesPrivateSettings.yaml da conta
    4. Abre Riot Client — loga automaticamente
    """
    global LCU_PORT, LCU_PASSWORD

    safe_print(f"  >> Login via sessão salva: {username}")

    close_riot_client_and_lol()
    time.sleep(2)
    clear_riot_client_data()

    if not restaurar_session(username, sessions_dir):
        safe_print(f"  Sem sessão salva para {username}. Use a opção 4 primeiro.")
        return None, None

    # Abre com --launch-product para iniciar o LoL e o LCU junto
    league_exe = find_league_client_exe()
    if league_exe:
        safe_print(f"  Lançando LeagueClient headless: {league_exe}")
        try:
            subprocess.Popen(
                [league_exe, "--headless"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            safe_print(f"  Erro ao lançar LeagueClient diretamente ({e}), usando RiotClientServices...")
            if not launch_riot_client(no_launch=False):
                safe_print("  Não foi possível abrir o Riot Client.")
                return None, None
    else:
        if not launch_riot_client(no_launch=False):
            safe_print("  Não foi possível abrir o Riot Client.")
            return None, None
        safe_print("  Não foi possível abrir o Riot Client.")
        return None, None

    time.sleep(5)

    if not wait_for_league_client(timeout=90):
        safe_print("  LeagueClient não iniciou em 90s.")
        return None, None

    port, password = wait_for_lcu_ready(timeout=60)
    if not port:
        safe_print("  LCU não ficou disponível.")
        return None, None

    LCU_PORT = port
    LCU_PASSWORD = password
    safe_print(f"  LCU pronto na porta {port}.")
    return port, password

# ─────────────────────────────────────────────
#  LOGIN COM PYAUTOGUI + SALVAR SESSAO
# ─────────────────────────────────────────────

def launch_and_login_save_session(username, user_password):
    """Login com pyautogui marcando checkbox 'Manter sessão'."""
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.05
    except ImportError:
        safe_print("Erro: pyautogui não instalado.")
        return None, None

    global LCU_PORT, LCU_PASSWORD

    safe_print(f"  >> Logando (salvar sessão): {username}")

    clear_riot_client_data()
    close_riot_client_and_lol()

    # Abre com --launch-product para lançar o LoL diretamente
    if not launch_riot_client(no_launch=False):
        safe_print("  Não foi possível abrir o Riot Client.")
        return None, None

    time.sleep(14)

    try:
        # Busca janela do Riot Client de forma robusta
        win = None
        for titulo in ["Riot Client", "Riot Client Main", "Riot Games"]:
            wins = pyautogui.getWindowsWithTitle(titulo)
            if wins:
                win = wins[0]
                break
        if not win:
            raise Exception("Janela do Riot Client não encontrada")
        win.activate()
        time.sleep(0.3)

        pyautogui.hotkey('ctrl', 'a')
        time.sleep(0.2)
        type_with_clipboard(username)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.3)

        pyautogui.press('tab')
        time.sleep(0.2)
        type_with_clipboard(user_password)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.3)

        # 6 tabs para chegar no checkbox "Manter sessão iniciada"
        pyautogui.press('tab', presses=6, interval=0.1)
        time.sleep(0.2)
        pyautogui.press('space')
        time.sleep(0.2)

        pyautogui.press('enter')
        safe_print(f"  Checkbox 'Manter sessão' marcado.")
    except Exception as e:
        safe_print(f"  Erro ao digitar credenciais: {e}")
        return None, None

    if not wait_for_league_client(timeout=80):
        safe_print("  LeagueClient não iniciou em 80s. Pulando.")
        return None, None

    port, password = wait_for_lcu_ready(timeout=60)
    if not port:
        safe_print("  LCU não ficou disponível.")
        return None, None

    LCU_PORT = port
    LCU_PASSWORD = password
    safe_print(f"  LCU pronto na porta {port}.")
    return port, password

def read_account_credentials(file_path):
    accounts = []
    try:
        with open(file_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and ':' in line:
                    username, password = line.split(':', 1)
                    accounts.append({'username': username, 'password': password})
    except FileNotFoundError:
        safe_print(f"Erro: Arquivo {file_path} não encontrado.")
    return accounts

# ─────────────────────────────────────────────
#  OPCAO 4 — SALVAR SESSOES
# ─────────────────────────────────────────────

def salvar_sessoes():
    print("\n" + "="*60)
    print("  SALVAR SESSÕES — Copiando RiotGamesPrivateSettings.yaml")
    print("="*60)

    arquivo = input("\n  Arquivo de contas (Enter para 'account_credentials.txt'): ").strip() or "account_credentials.txt"

    contas = read_account_credentials(arquivo)
    if not contas:
        safe_print(f"  Nenhuma conta encontrada em '{arquivo}'.")
        input("\nPressione Enter para sair...")
        return

    script_dir = os.path.dirname(os.path.abspath(__file__))
    sessions_dir = os.path.join(script_dir, "sessions")
    os.makedirs(sessions_dir, exist_ok=True)

    safe_print(f"\n  {len(contas)} contas para salvar sessão.\n")

    salvos = 0
    falhas = 0

    for account in contas:
        username = account["username"]
        user_password = account["password"]

        safe_print(f"\n>>> [{salvos+falhas+1}/{len(contas)}] Logando: {username} <<<")

        port, pwd = launch_and_login_save_session(username, user_password)
        if not port:
            safe_print(f"  FALHA ao logar em {username}.")
            falhas += 1
            close_riot_client_and_lol()
            continue

        # Aguardar o arquivo ser gerado
        time.sleep(3)

        settings_path = get_riot_settings_path()
        conta_session_dir = os.path.join(sessions_dir, username)
        os.makedirs(conta_session_dir, exist_ok=True)

        if settings_path:
            dest = os.path.join(conta_session_dir, RIOT_SETTINGS_FILENAME)
            try:
                shutil.copy2(settings_path, dest)
                safe_print(f"  RiotGamesPrivateSettings.yaml copiado.")

                session_info = {
                    "username": username,
                    "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "source_file": settings_path
                }
                with open(os.path.join(conta_session_dir, "session.json"), "w", encoding="utf-8") as f:
                    json.dump(session_info, f, ensure_ascii=False, indent=2)

                safe_print(f"  Sessão salva em: sessions/{username}/")
                salvos += 1
                deletar_yaml_data()
            except Exception as e:
                safe_print(f"  ERRO ao copiar arquivo: {e}")
                falhas += 1
        else:
            safe_print(f"  AVISO: RiotGamesPrivateSettings.yaml não encontrado.")
            falhas += 1

        # Fechar sem logout para preservar sessão
        close_riot_client_and_lol()

    safe_print(f"\n{'='*60}")
    safe_print(f"  RESULTADO")
    safe_print(f"{'='*60}")
    safe_print(f"  Sessões salvas : {salvos}")
    safe_print(f"  Falhas         : {falhas}")
    safe_print(f"  Pasta          : sessions/")

if __name__ == "__main__":
    salvar_sessoes()
    input("\nPressione Enter para fechar...")
