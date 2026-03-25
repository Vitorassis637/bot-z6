"""
check_accepted.py
-----------------
Roda separado do bot principal.
Checa quem aceitou o pedido de amizade e envia a mensagem do Discord.

Uso:
    python check_accepted.py

Precisa que o LeagueClient esteja aberto e logado.
"""

import json
import os
import base64
import time
import random
import urllib3
import requests
from datetime import datetime, timezone

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from lockutils import get_lcu_credentials
except ImportError:
    print("Erro: lockutils.py nao encontrado. Coloque este script na mesma pasta do bot.")
    input("Pressione Enter para sair...")
    exit(1)

ADDED_PLAYERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "added_players.json")
DISCORD_URL = "https://discord.gg/9TYwNpBQR2"

DISCORD_MESSAGES = [
    f"Você sente que joga melhor do que o seu elo? Muita gente fica presa no Elo Hell mesmo jogando bem. A gente só te ajuda a chegar no elo que você merece. {DISCORD_URL}",
    f"Você acha que tem nível mais alto que o seu elo atual? Às vezes o problema é só o Elo Hell. A gente te ajuda a chegar no elo que você realmente merece. {DISCORD_URL}",
    f"Você joga bem mas parece que nunca sai do mesmo elo? Isso acontece com muita gente por causa do matchmaking. A gente só ajuda você a chegar no elo que merece. {DISCORD_URL}",
    f"Você sente que poderia estar em um elo mais alto? Às vezes não é falta de skill, é só Elo Hell. A gente te ajuda a chegar onde você merece estar. {DISCORD_URL}",
]

LCU_PORT = None
LCU_PASSWORD = None


def get_lcu():
    global LCU_PORT, LCU_PASSWORD
    port, password = get_lcu_credentials()
    if port and password:
        LCU_PORT, LCU_PASSWORD = port, password
        return True
    return False


def lcu_request(method, endpoint, data=None, params=None):
    url = f"https://127.0.0.1:{LCU_PORT}{endpoint}"
    headers = {
        "Authorization": "Basic " + base64.b64encode(f"riot:{LCU_PASSWORD}".encode()).decode()
    }
    try:
        resp = requests.request(method, url, headers=headers, json=data, params=params, verify=False, timeout=10)
        if resp.status_code in (200, 201, 204):
            try:
                return resp.json()
            except Exception:
                return {}
        return None
    except Exception as e:
        print(f"  Erro LCU: {e}")
        return None


def get_friends_set():
    result = lcu_request("GET", "/lol-chat/v1/friends")
    if result and isinstance(result, list):
        return {f.get('gameName', '').lower() for f in result if f.get('gameName')}
    return set()


def get_friend_id(game_name, tag_line):
    friends = lcu_request("GET", "/lol-chat/v1/friends")
    if not friends:
        return None
    for f in friends:
        if f.get("gameName", "").lower() == game_name.lower():
            return f.get("id")
    return None


def send_message(game_name, tag_line, message):
    friend_id = get_friend_id(game_name, tag_line)
    if not friend_id:
        print(f"  Amigo {game_name}#{tag_line} nao encontrado.")
        return False

    result = lcu_request(
        "POST",
        f"/lol-chat/v1/conversations/{friend_id}/messages",
        data={"body": message, "type": "chat"}
    )
    return result is not None


def load_players():
    if not os.path.exists(ADDED_PLAYERS_FILE):
        print(f"Arquivo '{ADDED_PLAYERS_FILE}' nao encontrado.")
        return []
    try:
        with open(ADDED_PLAYERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Erro ao ler arquivo: {e}")
        return []


def save_players(players):
    try:
        with open(ADDED_PLAYERS_FILE, "w", encoding="utf-8") as f:
            json.dump(players, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Erro ao salvar arquivo: {e}")


def print_stats(players):
    total = len(players)
    accepted = sum(1 for p in players if p.get("accepted"))
    messaged = sum(1 for p in players if p.get("message_sent"))
    pending = total - accepted
    print(f"\n{'='*50}")
    print(f"  Total adicionados : {total}")
    print(f"  Aceitaram         : {accepted}")
    print(f"  Mensagem enviada  : {messaged}")
    print(f"  Pendentes         : {pending}")
    print(f"{'='*50}\n")


def main():
    print("=" * 50)
    print("  CHECK ACCEPTED — Verificador de aceitacoes")
    print("=" * 50)

    players = load_players()
    if not players:
        input("\nNenhum jogador para checar. Pressione Enter para sair...")
        return

    print_stats(players)

    print("Conectando ao LCU...")
    if not get_lcu():
        print("Erro: Nao foi possivel conectar ao LCU.")
        print("Certifique-se de que o League of Legends esta aberto e logado.")
        input("\nPressione Enter para sair...")
        return
    print(f"LCU conectado na porta {LCU_PORT}.\n")

    pending = [p for p in players if not p.get("accepted")]
    print(f"Checando {len(pending)} jogadores pendentes...")

    friends_set = get_friends_set()
    if not friends_set:
        print("Nao foi possivel obter lista de amigos.")
        input("\nPressione Enter para sair...")
        return

    novos_aceites = 0
    mensagens_enviadas = 0

    for player in players:
        if player.get("accepted"):
            # Tenta enviar mensagem se aceitou mas ainda nao recebeu
            if not player.get("message_sent"):
                msg = random.choice(DISCORD_MESSAGES)
                print(f"  Enviando mensagem pendente para {player['name']}#{player['tag']}...")
                ok = send_message(player["name"], player["tag"], msg)
                if ok:
                    player["message_sent"] = True
                    player["message_sent_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    mensagens_enviadas += 1
                time.sleep(0.5)
            continue

        key = f"{player['name']}#{player['tag']}"
        if player['name'].lower() in friends_set:
            player["accepted"] = True
            player["accepted_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            novos_aceites += 1
            print(f"  ACEITOU: {key}")

            if not player.get("message_sent"):
                msg = random.choice(DISCORD_MESSAGES)
                print(f"    Enviando mensagem: {msg[:60]}...")
                ok = send_message(player["name"], player["tag"], msg)
                if ok:
                    player["message_sent"] = True
                    player["message_sent_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    mensagens_enviadas += 1
                time.sleep(0.5)

    save_players(players)

    print(f"\n  Novos aceites     : {novos_aceites}")
    print(f"  Mensagens enviadas: {mensagens_enviadas}")
    print_stats(players)

    input("Pressione Enter para sair...")


if __name__ == "__main__":
    main()
