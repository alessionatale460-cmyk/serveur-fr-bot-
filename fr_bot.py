"""
Serveur FR — Bot Discord Minecraft
------------------------------------
Tout en un seul message épinglé :
  - Joueurs en ligne
  - Leaderboard quêtes
  - Classement temps de jeu
  - Statistiques joueurs
  - Notifications de paliers (channel séparé optionnel)
"""

import json
import os
import re
import ftplib
import io
from datetime import datetime

import discord
from discord.ext import tasks

# ─────────────────────────────────────────
#  CONFIG — à remplir via Secrets Railway
# ─────────────────────────────────────────

FTP_HOST     = os.environ.get("FTP_HOST_FR", "TON_HOST.dathost.net")
FTP_PORT     = 21
FTP_USER     = os.environ.get("FTP_USER_FR", "TON_USER")
FTP_PASSWORD = os.environ.get("FTP_PASSWORD_FR", "TON_PASSWORD")

FTBQUESTS_PATH = "/world/ftbquests/"
STATS_PATH     = "/world/stats/"

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN_FR", "TON_TOKEN")

# RCON
RCON_HOST     = os.environ.get("RCON_HOST", "")
RCON_PORT     = int(os.environ.get("RCON_PORT", "25575"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "")

# Un seul channel pour tout afficher
CHANNEL_DASHBOARD = int(os.environ.get("CHANNEL_DASHBOARD", "0"))

# Channel optionnel pour les notifications de paliers
CHANNEL_NOTIFS = int(os.environ.get("CHANNEL_NOTIFS", "0"))

# Port Minecraft pour joueurs en ligne
MC_SERVER_PORT = 17161

# Fréquence de mise à jour
UPDATE_INTERVAL_MINUTES = 60

# Paliers de notification
CHAPTER_MILESTONES = {
    50:   "⚙️ **{name}** a terminé ses premiers automatismes !\n┗ *50 tâches complétées — premier palier atteint !*",
    100:  "🔬 **{name}** maîtrise la technologie de base !\n┗ *100 tâches complétées — les choses sérieuses commencent !*",
    200:  "⚡ **{name}** entre dans le vif du sujet !\n┗ *200 tâches complétées — mi-chemin vers le sommet !*",
    350:  "🏆 **{name}** est dans le top tier du modpack !\n┗ *350 tâches complétées — plus grand chose à apprendre !*",
    500:  "🌟 **{name}** est une légende vivante !\n┗ *500 tâches complétées — respect absolu !*",
    750:  "🔥 **{name}** fait partie de l'élite !\n┗ *750 tâches complétées — peu de joueurs arrivent jusque là !*",
    1000: "💎 **{name}** a complété un quart du modpack !\n┗ *1000 tâches complétées — impressionnant !*",
    1500: "🚀 **{name}** a complété un tiers du modpack !\n┗ *1500 tâches complétées — inarrêtable !*",
    2000: "⚔️ **{name}** est à mi-chemin du modpack !\n┗ *2000 tâches complétées — halfway there !*",
    2500: "🌍 **{name}** a passé la moitié du modpack !\n┗ *2500 tâches complétées — plus que la descente !*",
    3000: "🎯 **{name}** est en ligne droite finale !\n┗ *3000 tâches complétées — le finish se rapproche !*",
    3500: "👑 **{name}** est presque au bout !\n┗ *3500 tâches complétées — plus que 538 tâches !*",
    4038: "🏅 **{name}** a terminé le modpack en entier !\n┗ *4038 tâches complétées — MODPACK 100% !* 🎉",
}


# Paliers de rangs par heures de jeu
RANK_MILESTONES = [
    {"hours": 0,  "rank": "nouveau",      "prefix": "&7[Nouveau]",     "chunks": 1,  "homes": 1},
    {"hours": 2,  "rank": "joueur",       "prefix": "&a[Joueur]",      "chunks": 5,  "homes": 2},
    {"hours": 5,  "rank": "habitue",      "prefix": "&9[Habitué]",     "chunks": 10, "homes": 3},
    {"hours": 10, "rank": "experimente",  "prefix": "&5[Expérimenté]", "chunks": 15, "homes": 4},
    {"hours": 20, "rank": "veteran",      "prefix": "&6[Vétéran]",     "chunks": 20, "homes": 6},
    {"hours": 40, "rank": "expert",       "prefix": "&c[Expert]",      "chunks": 30, "homes": 10},
    {"hours": 60, "rank": "legende",      "prefix": "&4[Légende]",     "chunks": 40, "homes": -1},
]

def get_rank_for_hours(hours: float) -> dict:
    """Retourne le rang correspondant aux heures de jeu."""
    current = RANK_MILESTONES[0]
    for milestone in RANK_MILESTONES:
        if hours >= milestone["hours"]:
            current = milestone
    return current


# ─────────────────────────────────────────
#  FTP
# ─────────────────────────────────────────

def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=15)
    ftp.login(FTP_USER, FTP_PASSWORD)
    ftp.set_pasv(True)
    return ftp

def ftp_read(ftp: ftplib.FTP, path: str) -> str:
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {path}", buf.write)
    return buf.getvalue().decode("utf-8", errors="ignore")

# ─────────────────────────────────────────
#  QUÊTES
# ─────────────────────────────────────────

def count_quests(content: str) -> int:
    match = re.search(r'task_progress:\s*\{([^}]*)\}', content, re.DOTALL)
    if not match:
        return 0
    return len(re.findall(r'[0-9A-Fa-f]{16}:\s*1L', match.group(1)))

def get_player_name(content: str, fallback: str) -> str:
    match = re.search(r'name:\s*"([^"#]+)', content)
    return match.group(1).strip() if match else fallback

# ─────────────────────────────────────────
#  STATS VANILLA
# ─────────────────────────────────────────

def parse_stats(content: str) -> dict:
    try:
        data = json.loads(content)
        stats = data.get("stats", data)

        def get(cat, key):
            return stats.get(cat, {}).get(key, 0)

        play_ticks   = get("minecraft:custom", "minecraft:play_time") or \
                       get("minecraft:custom", "minecraft:play_one_minute")
        deaths       = get("minecraft:custom", "minecraft:deaths")
        blocks_mined = sum(stats.get("minecraft:mined", {}).values())
        distance_cm  = (
            get("minecraft:custom", "minecraft:walk_one_cm") +
            get("minecraft:custom", "minecraft:sprint_one_cm") +
            get("minecraft:custom", "minecraft:fly_one_cm")
        )
        items_crafted = sum(stats.get("minecraft:crafted", {}).values())
        mobs_killed   = sum(stats.get("minecraft:killed", {}).values())

        return {
            "playtime_hours": round(play_ticks / 72000, 1),
            "deaths":         deaths,
            "blocks_mined":   blocks_mined,
            "distance_km":    round(distance_cm / 100000, 1),
            "items_crafted":  items_crafted,
            "mobs_killed":    mobs_killed,
        }
    except Exception:
        return {"playtime_hours": 0, "deaths": 0, "blocks_mined": 0, "distance_km": 0}

# ─────────────────────────────────────────
#  FETCH COMPLET
# ─────────────────────────────────────────

def fetch_all_players() -> list[dict]:
    players = {}
    ftp = ftp_connect()

    # Quêtes FTB
    try:
        ftp.cwd(FTBQUESTS_PATH)
        for filename in ftp.nlst():
            if not filename.endswith(".snbt"):
                continue
            uuid = filename.rsplit(".", 1)[0]
            try:
                content = ftp_read(ftp, filename)
                players[uuid] = {
                    "uuid":           uuid,
                    "name":           get_player_name(content, uuid[:12] + "..."),
                    "quests":         count_quests(content),
                    "playtime_hours": 0,
                    "deaths":         0,
                    "blocks_mined":   0,
                    "distance_km":    0,
                    "items_crafted":  0,
                    "mobs_killed":    0,
                }
            except Exception as e:
                print(f"[WARN] quests/{filename} : {e}")
    except Exception as e:
        print(f"[ERROR] FTB path : {e}")

    # Stats vanilla
    try:
        ftp.cwd(STATS_PATH)
        for filename in ftp.nlst():
            if not filename.endswith(".json"):
                continue
            uuid = filename.rsplit(".", 1)[0]
            try:
                content = ftp_read(ftp, filename)
                s = parse_stats(content)
                if uuid in players:
                    players[uuid].update(s)
                else:
                    players[uuid] = {"uuid": uuid, "name": uuid[:12] + "...",
                                     "quests": 0, "items_crafted": 0, "mobs_killed": 0, **s}
            except Exception as e:
                print(f"[WARN] stats/{filename} : {e}")
    except Exception as e:
        print(f"[ERROR] Stats path : {e}")

    ftp.quit()
    return list(players.values())

# ─────────────────────────────────────────
#  JOUEURS EN LIGNE
# ─────────────────────────────────────────

def get_online_count() -> tuple[int, int]:
    try:
        s = socket.create_connection((FTP_HOST, MC_SERVER_PORT), timeout=5)
        host_b    = FTP_HOST.encode("utf-8")
        handshake = (
            b"\x00"
            + b"\xff\xff\xff\xff\x0f"
            + bytes([len(host_b)]) + host_b
            + struct.pack(">H", MC_SERVER_PORT)
            + b"\x01"
        )
        length = len(handshake)
        s.sendall(bytes([length]) + handshake + b"\x01\x00")
        buf = b""
        while len(buf) < 5:
            chunk = s.recv(1024)
            if not chunk:
                break
            buf += chunk
        s.close()
        json_start = buf.find(b"{")
        if json_start == -1:
            return 0, 0
        status = json.loads(buf[json_start:buf.rfind(b"}") + 1])
        online  = status.get("players", {}).get("online", 0)
        maximum = status.get("players", {}).get("max", 0)
        return online, maximum
    except Exception:
        return -1, 0

# ─────────────────────────────────────────
#  EMBED UNIQUE
# ─────────────────────────────────────────

def build_dashboard(players: list[dict], online: int, maximum: int) -> discord.Embed:
    medals = ["🥇", "🥈", "🥉"]

    # Statut serveur
    embed = discord.Embed(
        title="🎮 Tableau de bord — Serveur FR",
        description="🟢 Serveur en ligne",
        color=0x2ECC71,
        timestamp=datetime.utcnow(),
    )

    # Leaderboard quêtes
    sorted_quests = sorted(players, key=lambda x: x["quests"], reverse=True)
    quests_text = ""
    for i, p in enumerate(sorted_quests[:10]):
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        quests_text += f"{medal} **{p['name']}** — {p['quests']} tâches\n"
    embed.add_field(
        name="🏆 Classement Quêtes",
        value=quests_text or "Aucun joueur",
        inline=False
    )

    # Classement temps de jeu
    sorted_time = sorted(players, key=lambda x: x["playtime_hours"], reverse=True)
    time_text = ""
    for i, p in enumerate(sorted_time[:10]):
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        time_text += f"{medal} **{p['name']}** — {p['playtime_hours']}h\n"
    embed.add_field(
        name="⏱️ Temps de jeu",
        value=time_text or "Aucun joueur",
        inline=False
    )

    # Statistiques détaillées
    stats_text = ""
    for p in sorted_quests[:10]:
        stats_text += (
            f"**{p['name']}** — "
            f"💀 {p['deaths']} morts · "
            f"⛏️ {p['blocks_mined']:,} blocs · "
            f"🚶 {p['distance_km']} km · "
            f"⚗️ {p['items_crafted']:,} crafts · "
            f"🐾 {p['mobs_killed']:,} mobs\n"
        )
    embed.add_field(
        name="📊 Statistiques",
        value=stats_text or "Aucune donnée",
        inline=False
    )

    embed.set_footer(text=f"Mis à jour toutes les {UPDATE_INTERVAL_MINUTES} min")
    return embed

# ─────────────────────────────────────────
#  RÉCAP HEBDOMADAIRE
# ─────────────────────────────────────────

# Channel pour le récap hebdomadaire
CHANNEL_RECAP = int(os.environ.get("CHANNEL_RECAP", "0"))
CHANNEL_RANKS = 1480899926803222742

# Snapshot de la semaine précédente { uuid: {quests, playtime_hours} }
last_week_snapshot: dict[str, dict] = {}


def build_recap(players: list[dict], snapshot: dict) -> discord.Embed:
    """Construit l'embed du récap hebdomadaire."""

    now = datetime.now()
    week_number = now.isocalendar()[1]

    embed = discord.Embed(
        title=f"📅 Récap de la semaine {week_number}",
        description="Voici ce qui s'est passé cette semaine sur le serveur !",
        color=0x9B59B6,
        timestamp=now,
    )

    # Meilleure progression quêtes
    best_progress = None
    best_progress_count = 0
    for p in players:
        prev = snapshot.get(p["uuid"], {}).get("quests", p["quests"])
        diff = p["quests"] - prev
        if diff > best_progress_count:
            best_progress_count = diff
            best_progress = p["name"]

    if best_progress and best_progress_count > 0:
        embed.add_field(
            name="🏆 Meilleure progression",
            value=f"**{best_progress}** — +{best_progress_count} tâches cette semaine !",
            inline=False
        )
    else:
        embed.add_field(
            name="🏆 Meilleure progression",
            value="Aucune progression cette semaine.",
            inline=False
        )

    # Joueur le plus actif
    most_active = None
    most_active_hours = 0.0
    for p in players:
        prev = snapshot.get(p["uuid"], {}).get("playtime_hours", p["playtime_hours"])
        diff = round(p["playtime_hours"] - prev, 1)
        if diff > most_active_hours:
            most_active_hours = diff
            most_active = p["name"]

    if most_active and most_active_hours > 0:
        embed.add_field(
            name="⏱️ Joueur le plus actif",
            value=f"**{most_active}** — {most_active_hours}h de jeu cette semaine !",
            inline=False
        )
    else:
        embed.add_field(
            name="⏱️ Joueur le plus actif",
            value="Aucune activité cette semaine.",
            inline=False
        )

    # Classement global actuel
    sorted_players = sorted(players, key=lambda x: x["quests"], reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    ranking_text = ""
    for i, p in enumerate(sorted_players[:5]):
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        ranking_text += f"{medal} **{p['name']}** — {p['quests']} tâches\n"

    embed.add_field(
        name="📊 Classement actuel",
        value=ranking_text or "Aucun joueur",
        inline=False
    )

    embed.set_footer(text="Récap posté automatiquement chaque dimanche soir")
    return embed


def save_snapshot(players: list[dict]):
    """Sauvegarde le snapshot actuel pour comparaison la semaine suivante."""
    global last_week_snapshot
    last_week_snapshot = {
        p["uuid"]: {
            "quests":         p["quests"],
            "playtime_hours": p["playtime_hours"],
        }
        for p in players
    }


@tasks.loop(minutes=60)
async def check_weekly_recap():
    """Vérifie chaque heure si c'est dimanche soir pour poster le récap."""
    now = datetime.now()
    # Dimanche = 6, entre 20h et 21h
    if now.weekday() != 6 or now.hour != 20:
        return

    if not CHANNEL_RECAP:
        print("[WARN] CHANNEL_RECAP non configuré.")
        return

    channel = client.get_channel(CHANNEL_RECAP)
    if not channel:
        print("[ERROR] Channel récap introuvable.")
        return

    print(f"[INFO] Publication du récap hebdomadaire...")

    try:
        players = fetch_all_players()
    except Exception as e:
        print(f"[ERROR] fetch récap : {e}")
        return

    embed = build_recap(players, last_week_snapshot)
    await channel.send(embed=embed)
    save_snapshot(players)
    print("[OK] Récap hebdomadaire posté !")



# ─────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────

intents  = discord.Intents.default()
intents.message_content = True
client   = discord.Client(intents=intents)
tree     = discord.app_commands.CommandTree(client)
msg_id   = None
notified: dict[str, set] = {}


@tasks.loop(minutes=UPDATE_INTERVAL_MINUTES)
async def update_dashboard():
    global msg_id

    if not CHANNEL_DASHBOARD:
        print("[ERROR] CHANNEL_DASHBOARD non configuré.")
        return

    channel = client.get_channel(CHANNEL_DASHBOARD)
    if not channel:
        print("[ERROR] Channel introuvable.")
        return

    print(f"[INFO] Mise à jour ({datetime.now().strftime('%H:%M:%S')})...")

    try:
        players = fetch_all_players()
    except Exception as e:
        print(f"[ERROR] fetch : {e}")
        return

    online, maximum = get_online_count()
    embed = build_dashboard(players, online, maximum)

    # Mettre à jour ou créer le message
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed)
            print(f"[OK] Dashboard mis à jour ({len(players)} joueurs).")
        except discord.NotFound:
            msg_id = None

    if not msg_id:
        msg = await channel.send(embed=embed)
        msg_id = msg.id
        await msg.pin()
        print(f"[OK] Dashboard créé et épinglé (ID: {msg.id}).")

    # Notifications de paliers
    if CHANNEL_NOTIFS:
        notif_channel = client.get_channel(CHANNEL_NOTIFS)
        if notif_channel:
            for p in players:
                uuid = p["uuid"]
                if uuid not in notified:
                    notified[uuid] = set()
                for threshold, message in CHAPTER_MILESTONES.items():
                    if p["quests"] >= threshold and threshold not in notified[uuid]:
                        notified[uuid].add(threshold)
                        await notif_channel.send(message.format(name=p["name"]))


def build_ranks_embed() -> discord.Embed:
    """Construit l'embed d'info sur les rangs."""
    embed = discord.Embed(
        title="🏅 Système de Rangs",
        description="Plus tu joues, plus tu débloques d'avantages !\nLes rangs sont attribués automatiquement selon ton temps de jeu.",
        color=0x9B59B6,
    )
    for m in RANK_MILESTONES:
        homes = "∞" if m["homes"] == -1 else str(m["homes"])
        embed.add_field(
            name=f"{m['prefix'].replace('&7','').replace('&a','').replace('&9','').replace('&5','').replace('&6','').replace('&c','').replace('&4','')}  —  {m['hours']}h",
            value=f"⛏️ {m['chunks']} chunks · 🏠 {homes} homes",
            inline=False
        )
    embed.set_footer(text="Les rangs sont vérifiés toutes les 30 minutes")
    return embed


@client.event
async def on_message(message):
    if message.author.bot:
        return

    if message.content.startswith("!rang"):
        # Récupère le pseudo Minecraft depuis l'argument ou le nom Discord
        parts = message.content.split()
        if len(parts) > 1:
            target_name = parts[1]
        else:
            target_name = message.author.display_name

        try:
            players = fetch_all_players()
        except Exception as e:
            await message.channel.send(f"❌ Erreur lors de la récupération des données : {e}")
            return

        # Cherche le joueur
        player = next((p for p in players if p["name"].lower() == target_name.lower()), None)

        if not player:
            await message.channel.send(
                f"❌ Joueur **{target_name}** introuvable. Utilise `!rang <pseudo_minecraft>`"
            )
            return

        hours = player["playtime_hours"]
        current_rank = get_rank_for_hours(hours)
        prefix_clean = current_rank["prefix"].replace("&7","").replace("&a","").replace("&9","").replace("&5","").replace("&6","").replace("&c","").replace("&4","")

        # Prochain rang
        next_rank = None
        for m in RANK_MILESTONES:
            if m["hours"] > hours:
                next_rank = m
                break

        embed = discord.Embed(
            title=f"🎮 Progression de {player['name']}",
            color=0x9B59B6,
        )
        embed.add_field(name="Rang actuel", value=prefix_clean, inline=True)
        embed.add_field(name="Temps de jeu", value=f"{hours}h", inline=True)
        embed.add_field(name="Quêtes", value=f"{player['quests']} tâches", inline=True)

        if next_rank:
            hours_left = round(next_rank["hours"] - hours, 1)
            next_prefix = next_rank["prefix"].replace("&7","").replace("&a","").replace("&9","").replace("&5","").replace("&6","").replace("&c","").replace("&4","")
            homes_next = "∞" if next_rank["homes"] == -1 else str(next_rank["homes"])
            embed.add_field(
                name="⏭️ Prochain rang",
                value=f"{next_prefix} dans **{hours_left}h**\n⛏️ {next_rank['chunks']} chunks · 🏠 {homes_next} homes",
                inline=False
            )

            # Barre de progression
            total = next_rank["hours"] - current_rank["hours"]
            done = hours - current_rank["hours"]
            pct = min(int((done / total) * 20), 20) if total > 0 else 20
            bar = "█" * pct + "░" * (20 - pct)
            embed.add_field(
                name="Progression",
                value=f"`{bar}` {round((done/total)*100 if total > 0 else 100)}%",
                inline=False
            )
        else:
            embed.add_field(name="⭐ Rang maximum atteint !", value="Tu as débloqué tous les avantages.", inline=False)

        await message.channel.send(embed=embed)




def rcon_send(command: str) -> str:
    """Envoie une commande RCON au serveur Minecraft."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((RCON_HOST, RCON_PORT))

        def send_packet(req_id, req_type, payload):
            payload_bytes = payload.encode("utf-8") + b"\x00\x00"
            length = 4 + 4 + len(payload_bytes)
            packet = struct.pack("<iii", length, req_id, req_type) + payload_bytes
            sock.sendall(packet)

        def recv_packet():
            raw = b""
            while len(raw) < 4:
                raw += sock.recv(4096)
            length = struct.unpack("<i", raw[:4])[0]
            while len(raw) < 4 + length:
                raw += sock.recv(4096)
            req_id, req_type = struct.unpack("<ii", raw[4:12])
            payload = raw[12:4 + length - 2].decode("utf-8", errors="ignore")
            return req_id, req_type, payload

        send_packet(1, 3, RCON_PASSWORD)
        recv_packet()
        send_packet(2, 2, command)
        _, _, response = recv_packet()
        sock.close()
        return response
    except Exception as e:
        print(f"[RCON ERROR] {e}")
        return ""


def apply_rank(player_name: str, rank: dict):
    """Applique le rang FTB Ranks au joueur via RCON."""
    rcon_send(f"ftbranks set {player_name} {rank['rank']}")
    print(f"[RCON] Rang '{rank['rank']}' appliqué à {player_name}")


# Suivi des rangs déjà attribués { uuid: rank_name }
player_ranks: dict[str, str] = {}


@tasks.loop(minutes=30)
async def check_ranks():
    """Vérifie toutes les 30 min si des joueurs ont changé de rang."""
    if not RCON_HOST or not RCON_PASSWORD:
        print("[WARN] RCON non configuré, vérification des rangs ignorée.")
        return

    print("[INFO] Vérification des rangs...")
    try:
        players = fetch_all_players()
    except Exception as e:
        print(f"[ERROR] fetch rangs : {e}")
        return

    for p in players:
        rank = get_rank_for_hours(p["playtime_hours"])
        current = player_ranks.get(p["uuid"])
        if current != rank["rank"]:
            apply_rank(p["name"], rank)
            player_ranks[p["uuid"]] = rank["rank"]
            print(f"[RANK] {p['name']} → {rank['rank']} ({p['playtime_hours']}h)")


@client.event
async def on_ready():
    print(f"[OK] Bot connecté : {client.user}")
    guild = discord.Object(id=1480817204751634522)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print("[OK] Slash commands synchronisées sur le serveur.")
    update_dashboard.start()
    check_weekly_recap.start()
    check_ranks.start()

    # Poster/mettre à jour le message d'info rangs
    channel = client.get_channel(CHANNEL_RANKS)
    if channel:
        embed = build_ranks_embed()
        # Cherche un message existant du bot à mettre à jour
        found = False
        async for msg in channel.history(limit=10):
            if msg.author == client.user:
                await msg.edit(embed=embed)
                found = True
                break
        if not found:
            await channel.send(embed=embed)
        print("[OK] Channel #rangs mis à jour.")


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)




if __name__ == "__main__":
    client.run(DISCORD_TOKEN)




if __name__ == "__main__":
    client.run(DISCORD_TOKEN)




# ─────────────────────────────────────────
#  RCON
# ─────────────────────────────────────────

import socket
import struct




if __name__ == "__main__":
    client.run(DISCORD_TOKEN)




# ─────────────────────────────────────────
#  RCON
# ─────────────────────────────────────────

import socket
import struct



if __name__ == "__main__":
    client.run(DISCORD_TOKEN)




# ─────────────────────────────────────────
#  RCON
# ─────────────────────────────────────────

import socket
import struct
