
import os
import re
import base64
import psutil

def parse_lockfile(lockfile_path):
    with open(lockfile_path, 'r') as f:
        lockfile_content = f.read()

    match = re.search(r'^(?P<process_name>.+):(?P<pid>\d+):(?P<port>\d+):(?P<password>.+):(?P<protocol>.+)$', lockfile_content)
    if not match:
        print("Erro: Não foi possível analisar o conteúdo do lockfile.")
        return None, None

    port = match.group('port')
    password = match.group('password')
    return port, password

def get_lcu_credentials():
    # Tenta encontrar o caminho do processo LeagueClient.exe
    for proc in psutil.process_iter(['name', 'exe']):
        if proc.info['name'] == 'LeagueClient.exe':
            league_path = os.path.dirname(proc.info['exe'])
            lockfile_path = os.path.join(league_path, 'lockfile')
            if os.path.exists(lockfile_path):
                print(f"Lockfile encontrado em: {lockfile_path}")
                return parse_lockfile(lockfile_path)

    # Tenta caminhos padrão caso o processo não seja encontrado (ou psutil falhe)
    possible_paths = [
        os.path.join(os.environ.get('SystemDrive', 'C:'), 'Riot Games', 'League of Legends', 'lockfile'),
        os.path.join(os.getenv('LOCALAPPDATA', ''), 'Riot Games', 'League of Legends', 'lockfile'),
    ]
    
    lockfile_path = None
    for path in possible_paths:
        if os.path.exists(path):
            lockfile_path = path
            break
            
    if not lockfile_path:
        print("Erro: Arquivo lockfile não encontrado nos locais padrão.")
        print("Certifique-se de que o cliente do League of Legends esteja em execução.")
        user_path = input("Por favor, digite o caminho completo para o arquivo 'lockfile' (ou deixe em branco para sair): ")
        if user_path and os.path.exists(user_path):
            lockfile_path = user_path
        else:
            return None, None

    return parse_lockfile(lockfile_path)
