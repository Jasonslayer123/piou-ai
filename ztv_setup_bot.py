# ZTV Clan - Auto Server Builder
# 1. Create empty server in Discord (10 sec): + Add Server > Create My Own > Name: ZTV | Rivals Clan
# 2. Create a bot at https://discord.com/developers/applications > Bot > Reset Token > Copy
# 3. Invite bot: OAuth2 > URL Generator > check bot + administrator > open URL > invite to your server
# 4. Run: pip install discord.py ; python ztv_setup_bot.py
# Bot will build all roles/channels for you like ACE Rivals.

import discord
from discord.ext import commands
import os

TOKEN = os.getenv("DISCORD_BOT_TOKEN") or input("Paste your BOT TOKEN (not shared with anyone): ").strip()
GUILD_ID = int(os.getenv("GUILD_ID") or input("Paste your Server ID (right-click server > Copy Server ID, enable Developer Mode first): ").strip())

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

ROLES = [
    ("ZTV Owner", discord.Color.red(), discord.Permissions(administrator=True)),
    ("Co-Owner", discord.Color.orange(), discord.Permissions(administrator=True)),
    ("Head Scout", discord.Color.gold(), discord.Permissions(manage_messages=True, manage_roles=True, mute_members=True)),
    ("Scout / Tryout Host", discord.Color.green(), discord.Permissions(manage_messages=True, mute_members=True)),
    ("ZTV Member", discord.Color.blue(), discord.Permissions(send_messages=True, connect=True)),
    ("Verified", discord.Color.light_grey(), discord.Permissions(send_messages=True)),
    ("Tryouts", discord.Color.dark_grey(), discord.Permissions(send_messages=True)),
]

STRUCTURE = {
    "INFORMATION": [
        ("welcome", "Welcome + rules for ZTV"),
        ("announcements", "Staff only announcements"),
        ("tryout-info", "How tryouts work"),
        ("link-roblox", "Verify with Bloxlink here"),
    ],
    "COMMUNITY": ["general", "clips", "giveaways"],
    "TRYOUTS": ["scouting", "tryout-results"],
    "ZTV ONLY": ["clan-chat", "strategies", "roster"],
}

WELCOME_TEXT = """🔪 WELCOME TO ZTV — RIVALS CLAN 🔪
Welcome to ZTV, inspired by ACE #1 Knife Clan.

**How to join:**
1. Link Roblox in #link-roblox (use Bloxlink)
2. Get Tryouts role
3. Watch #scouting for hosts
4. 1v1 Scout: Stage 1 > Stage 2 > Final vs Owner = ZTV Member

No cheating, no toxicity, respect scouts. Good luck!"""

TRYOUT_TEXT = """🔪 ZTV TRYOUT SYSTEM (like ACE) 🔪
**Stage 1 - Scouting:** 1v1 vs Scout in #scouting VC. Knife only. Show movement + aim.
**Stage 2 - Consistency:** Best of 3 vs Head Scout.
**Stage 3 - Final:** 1v1 vs Owner/Co-Owner. Win or impress = in.

Hosts ping @Tryouts when live. Join fast, spots fill."""

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print("Guild not found. Check GUILD_ID and that bot is invited.")
        await bot.close(); return

    print(f"Building ZTV in: {guild.name}")

    # 1. Create roles
    existing = {r.name: r for r in guild.roles}
    created_roles = {}
    for name, color, perms in ROLES:
        if name in existing:
            created_roles[name] = existing[name]
        else:
            created_roles[name] = await guild.create_role(name=name, color=color, permissions=perms, mentionable=(name=="Tryouts"))
            print(f"+ Role {name}")

    member_role = created_roles["ZTV Member"]

    # 2. Delete default channels (optional clean)
    for ch in guild.channels:
        try: await ch.delete()
        except: pass

    # 3. Create categories + channels
    for cat_name, channels in STRUCTURE.items():
        cat = await guild.create_category(cat_name)
        print(f"+ Category {cat_name}")
        for c in channels:
            cname = c[0] if isinstance(c, tuple) else c
            ch = await guild.create_text_channel(cname, category=cat)
            print(f"  + #{cname}")
            if cname == "welcome":
                await ch.send(WELCOME_TEXT)
            if cname == "tryout-info":
                await ch.send(TRYOUT_TEXT)
            if cname in ["announcements", "tryout-results"]:
                await ch.set_permissions(guild.default_role, send_messages=False)
            if cat_name == "ZTV ONLY":
                await ch.set_permissions(guild.default_role, view_channel=False)
                await ch.set_permissions(member_role, view_channel=True)

    # Voice
    vv = await guild.create_category("VOICE")
    for vc in ["Lobby", "Scouting VC 1", "Scouting VC 2", "Clan VC"]:
        await guild.create_voice_channel(vc, category=vv)

    print("DONE! Add bots: Bloxlink, Carl-bot, MEE6. Then Server Settings > Enable Onboarding + Server Template.")
    await bot.close()

bot.run(TOKEN)
