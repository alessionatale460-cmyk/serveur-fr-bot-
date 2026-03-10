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
    50:  "⚙️ **{name}** a terminé ses premiers automatismes !\n┗ *50 tâches complétées — premier palier atteint !*",
    100: "🔬 **{name}** maîtrise la technologie de base !\n┗ *100 tâches complétées — les choses sérieuses commencent !*",
    200: "⚡ **{name}** entre dans le vif du sujet !\n┗ *200 tâches complétées — mi-chemin vers le sommet !*",
    350: "🏆 **{name}** est dans le top tier du modpack !\n┗ *350 tâches complétées — plus grand chose à apprendre !*",
    500: "🌟 **{name}** est une légende vivante !\n┗ *500 tâches complétées — respect absolu !*",
}

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
        return {
            "playtime_hours": round(play_ticks / 72000, 1),
            "deaths":         deaths,
            "blocks_mined":   blocks_mined,
            "distance_km":    round(distance_cm / 100000, 1),
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
                                     "quests": 0, **s}
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
            f"🚶 {p['distance_km']} km\n"
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
client   = discord.Client(intents=intents)
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


@client.event
async def on_ready():
    print(f"[OK] Bot connecté : {client.user}")
    update_dashboard.start()
    check_weekly_recap.start()


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
