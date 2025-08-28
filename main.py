import os
import telebot
from telebot import types
import requests
from dotenv import load_dotenv
import json
import sqlite3
from datetime import datetime, timedelta
import jdatetime
from collections import defaultdict
import pandas as pd
import matplotlib
matplotlib.use('Agg')  # ✅ باید قبل از pyplot باشد
import matplotlib.pyplot as plt
from io import BytesIO

# ------------------ ENV & BOT SETUP ------------------
load_dotenv()
BOT_TOKEN = os.environ.get('BOT_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
ADMIN_USER_ID = int(os.environ.get('ADMIN_USER_ID'))

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
HEADERS = {'Content-Type': 'application/json', 'x-goog-api-key': GEMINI_API_KEY}

bot = telebot.TeleBot(BOT_TOKEN)

# ------------------ DATABASE INIT ------------------
def init_db():
    conn = sqlite3.connect('expenses.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, amount REAL NOT NULL, category TEXT NOT NULL, note TEXT, timestamp DATETIME NOT NULL)')
    cursor.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, join_date DATETIME NOT NULL, last_seen_shamsi_month INTEGER DEFAULT 0)')
    cursor.execute('CREATE TABLE IF NOT EXISTS budgets (user_id INTEGER NOT NULL, year INTEGER NOT NULL, month INTEGER NOT NULL, amount REAL NOT NULL, last_alert INTEGER DEFAULT 0, PRIMARY KEY (user_id, year, month))')
    cursor.execute('CREATE TABLE IF NOT EXISTS user_state (user_id INTEGER PRIMARY KEY, last_expense_id INTEGER, edit_state_json TEXT)')
    try:
        cursor.execute('ALTER TABLE users ADD COLUMN last_seen_shamsi_month INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    print("✅ Database initialized")

# ------------------ HELPERS ------------------
def normalize_amount(text_amount):
    if not text_amount: return None
    persian_to_latin = str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789')
    text = text_amount.translate(persian_to_latin).replace('تومان', '').replace('تومن', '').replace('ریال', '').replace(',', '').strip()
    multiplier = 1
    if 'میلیون' in text:
        multiplier = 1_000_000
        text = text.replace('میلیون', '').strip()
    elif 'هزار' in text:
        multiplier = 1_000
        text = text.replace('هزار', '').strip()
    try:
        return float(text) * multiplier
    except ValueError:
        return None

def call_gemini_api(prompt):
    try:
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        response = requests.post(GEMINI_URL, headers=HEADERS, json=payload, timeout=15)
        if response.status_code == 200:
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        else:
            print(f"❌ Gemini API Error: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"❌ Gemini Error: {e}")
        return None

def get_user_state(cursor, user_id):
    cursor.execute("SELECT last_expense_id, edit_state_json FROM user_state WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    if result:
        edit_state = json.loads(result[1]) if result[1] else None
        return {'last_expense_id': result[0], 'edit_state': edit_state}
    return {'last_expense_id': None, 'edit_state': None}

def set_user_state(user_id, last_expense_id="UNCHANGED", edit_state="UNCHANGED"):
    conn = sqlite3.connect('expenses.db', check_same_thread=False)
    cursor = conn.cursor()
    current_state = get_user_state(cursor, user_id)
    final_last_expense_id = current_state['last_expense_id'] if last_expense_id == "UNCHANGED" else last_expense_id
    final_edit_state_json = json.dumps(current_state['edit_state']) if edit_state == "UNCHANGED" else (json.dumps(edit_state) if edit_state else None)
    cursor.execute("""
        INSERT INTO user_state (user_id, last_expense_id, edit_state_json) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            last_expense_id = excluded.last_expense_id,
            edit_state_json = excluded.edit_state_json
    """, (user_id, final_last_expense_id, final_edit_state_json))
    conn.commit()
    conn.close()

def get_shamsi_month_range(j_year, j_month):
    """
    یک سال و ماه شمسی می‌گیرد و بازه زمانی میلادی آن را برمی‌گرداند
    """
    start_of_month_j = jdatetime.datetime(j_year, j_month, 1)
    if j_month == 12:
        end_of_month_j = jdatetime.datetime(j_year + 1, 1, 1)
    else:
        end_of_month_j = jdatetime.datetime(j_year, j_month + 1, 1)
    
    return start_of_month_j.togregorian(), end_of_month_j.togregorian()

def check_for_new_shamsi_month(user_id):
    """
    ✅ --- FEATURE ENHANCEMENT ---
    بررسی می‌کند آیا ماه شمسی جدیدی شروع شده است یا خیر.
    اگر شروع شده باشد، گزارش ماه قبل را ارسال کرده و کاربر را برای تنظیم بودجه جدید راهنمایی می‌کند.
    """
    current_shamsi_month = jdatetime.datetime.now().month
    conn = sqlite3.connect('expenses.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT last_seen_shamsi_month FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    last_seen_month = result[0] if result else 0

    if last_seen_month != 0 and last_seen_month != current_shamsi_month:
        j_now = jdatetime.datetime.now()
        # محاسبه سال و ماه شمسی ماه گذشته
        last_month_j_year = j_now.year if j_now.month > 1 else j_now.year - 1
        last_month_j_month = j_now.month - 1 if j_now.month > 1 else 12
        
        last_month_j_date = jdatetime.datetime(last_month_j_year, last_month_j_month, 1)
        start_g, end_g = get_shamsi_month_range(last_month_j_year, last_month_j_month)

        # گرفتن بودجه و هزینه‌های ماه گذشته
        cursor.execute("SELECT amount FROM budgets WHERE user_id = ? AND year = ? AND month = ?", (user_id, last_month_j_year, last_month_j_month))
        last_month_budget_data = cursor.fetchone()
        
        cursor.execute("SELECT SUM(amount) FROM expenses WHERE user_id = ? AND timestamp >= ? AND timestamp < ?", (user_id, start_g, end_g))
        last_month_spent = cursor.fetchone()[0] or 0
        
        report_message = f"📅 ماه **{last_month_j_date.strftime('%B')}** به پایان رسید!\n\n✨ **خلاصه عملکرد شما:**\n"
        if last_month_budget_data:
            last_budget = last_month_budget_data[0]
            remaining = last_budget - last_month_spent
            report_message += (f"💰 بودجه: `{last_budget:,.0f}` تومان\n"
                               f"🧾 مجموع خرج: `{last_month_spent:,.0f}` تومان\n"
                               f"{'🟢' if remaining >= 0 else '🔴'} وضعیت نهایی: `{remaining:,.0f}` تومان\n\n")
        else:
            report_message += f"🧾 مجموع خرج: `{last_month_spent:,.0f}` تومان (بودجه‌ای تنظیم نشده بود).\n\n"
        
        report_message += f"حالا بودجه ماه جدید، **{j_now.strftime('%B')}**، را با دستور /setbudget تعیین کن."
        bot.send_message(user_id, report_message, parse_mode='Markdown')
        
    # در هر صورت، ماه دیده شده را به‌روزرسانی کن
    if last_seen_month != current_shamsi_month:
        cursor.execute("UPDATE users SET last_seen_shamsi_month = ? WHERE user_id = ?", (current_shamsi_month, user_id))
        conn.commit()

    conn.close()

def check_budget_alerts(user_id):
    """
    ✅ --- BUG FIX ---
    هشدارها را بر اساس بازه زمانی دقیق ماه شمسی بررسی می‌کند.
    """
    j_now = jdatetime.datetime.now()
    start_g, end_g = get_shamsi_month_range(j_now.year, j_now.month)
    
    conn = sqlite3.connect('expenses.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT amount, last_alert FROM budgets WHERE user_id = ? AND year = ? AND month = ?", (user_id, j_now.year, j_now.month))
    budget_data = cursor.fetchone()
    if not budget_data: conn.close(); return
    budget_amount, last_alert = budget_data
    
    cursor.execute("SELECT SUM(amount) FROM expenses WHERE user_id = ? AND timestamp >= ? AND timestamp < ?", (user_id, start_g, end_g))
    total_spent = cursor.fetchone()[0] or 0
    percentage = (total_spent / budget_amount) * 100 if budget_amount > 0 else 0
    alert_msg = None
    new_alert = last_alert
    
    if percentage >= 100 and last_alert < 100:
        alert_msg = f"🚨 بودجه شما تمام شد! ({percentage:.0f}٪ مصرف شده)"
        new_alert = 100
    elif percentage >= 80 and last_alert < 80:
        alert_msg = f"⚠️ ۸۰٪ بودجه مصرف شد. ({percentage:.0f}٪)"
        new_alert = 80
    elif percentage >= 50 and last_alert < 50:
        alert_msg = f"🔔 نصف بودجه مصرف شد. ({percentage:.0f}٪)"
        new_alert = 50
    
    if alert_msg:
        bot.send_message(user_id, alert_msg)
        cursor.execute("UPDATE budgets SET last_alert = ? WHERE user_id = ? AND year = ? AND month = ?", (new_alert, user_id, j_now.year, j_now.month))
        conn.commit()
        
    conn.close()

def save_expense(user_id, amount, category, note):
    conn = sqlite3.connect('expenses.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO expenses (user_id, amount, category, note, timestamp) VALUES (?, ?, ?, ?, ?)", (user_id, amount, category, note, datetime.now()))
    expense_id = cursor.lastrowid
    conn.commit()
    conn.close()
    set_user_state(user_id, last_expense_id=expense_id, edit_state=None)
    output_message = (f"✅ هزینه ثبت شد:\n\n💰 **مبلغ:** {amount:,.0f} تومان\n📂 **دسته:** {category}\n📝 **توضیحات:** {note}")
    bot.send_message(user_id, output_message, parse_mode='Markdown')
    check_budget_alerts(user_id)

# ------------------ BOT COMMANDS ------------------

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    user = message.from_user
    try:
        conn = sqlite3.connect('expenses.db', check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,))
        if cursor.fetchone() is None:
            cursor.execute("INSERT INTO users (user_id, first_name, username, join_date, last_seen_shamsi_month) VALUES (?, ?, ?, ?, ?)", (user.id, user.first_name, user.username, datetime.now(), 0)) # ثبت اولیه با 0
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Error registering user: {e}")
    
    check_for_new_shamsi_month(message.from_user.id)
    
    welcome_text = """
👋 سلام! به جیب‌جیب خوش اومدی 😊
اینجا می‌تونی دخل و خرجت رو راحت مدیریت کنی.

*دستورات اصلی:*
📊 *گزارش روزانه:* /reportdaily
📅 *گزارش هفتگی:* /reportweekly
💰 *تنظیم بودجه:* /setbudget
📈 *وضعیت بودجه:* /budget
↩️ *آخرین تراکنش:* /undo
📤 *خروجی اکسل و نمودار:* /export
🗑️ *پاک کردن سوابق:* /reset
ℹ️ *راهنما:* /help
"""
    bot.reply_to(message, welcome_text, parse_mode='Markdown')

@bot.message_handler(commands=['reportdaily', 'reportweekly'])
def handle_report(message):
    check_for_new_shamsi_month(message.from_user.id)
    user_id = message.from_user.id
    period = 'weekly' if 'weekly' in message.text else 'daily'
    title = "📅 گزارش هفتگی شما" if period == 'weekly' else "📊 گزارش روزانه شما"
    date_filter = "timestamp >= DATE('now', 'localtime', '-6 days')" if period == 'weekly' else "DATE(timestamp) = DATE('now', 'localtime')"
    conn = sqlite3.connect('expenses.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(f"SELECT category, SUM(amount) FROM expenses WHERE user_id = ? AND {date_filter} GROUP BY category ORDER BY SUM(amount) DESC", (user_id,))
    results = cursor.fetchall()
    conn.close()
    if not results:
        bot.send_message(user_id, "هیچ هزینه‌ای در این بازه ثبت نشده است."); return
    total_spent = sum(item[1] for item in results)
    report_text = f"*{title}*\n\n"
    for category, amount in results:
        report_text += f"📂 {category}: `{amount:,.0f}` تومان\n"
    report_text += f"\n💰 *مجموع:* `{total_spent:,.0f}` تومان"
    bot.send_message(user_id, report_text, parse_mode='Markdown')

@bot.message_handler(commands=['setbudget'])
def handle_set_budget(message):
    check_for_new_shamsi_month(message.from_user.id)
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        amount = normalize_amount(parts[1])
        if amount and amount > 0:
            j_now = jdatetime.datetime.now()
            conn = sqlite3.connect('expenses.db', check_same_thread=False)
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO budgets (user_id, year, month, amount, last_alert) VALUES (?, ?, ?, ?, 0)", (message.from_user.id, j_now.year, j_now.month, amount))
            conn.commit(); conn.close()
            bot.reply_to(message, f"✅ بودجه ماه {j_now.strftime('%B')} روی {amount:,.0f} تومان تنظیم شد.")
        else:
            bot.reply_to(message, f"❌ مبلغ '{parts[1]}' نامعتبر است.")
    else:
        msg = bot.reply_to(message, "بودجه این ماه چقدر باشد؟ 💰 (فقط عدد را ارسال کنید)")
        bot.register_next_step_handler(msg, process_budget_amount)

def process_budget_amount(message):
    amount = normalize_amount(message.text)
    if amount and amount > 0:
        j_now = jdatetime.datetime.now()
        conn = sqlite3.connect('expenses.db', check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO budgets (user_id, year, month, amount, last_alert) VALUES (?, ?, ?, ?, 0)", (message.from_user.id, j_now.year, j_now.month, amount))
        conn.commit(); conn.close()
        bot.reply_to(message, f"✅ بودجه ماه {j_now.strftime('%B')} روی {amount:,.0f} تومان تنظیم شد.")
    else:
        msg = bot.reply_to(message, "❌ ورودی نامعتبر است. دوباره فقط مبلغ را ارسال کنید.")
        bot.register_next_step_handler(msg, process_budget_amount)

@bot.message_handler(commands=['budget'])
def handle_budget_status(message):
    """
    ✅ --- BUG FIX ---
    وضعیت بودجه را بر اساس بازه زمانی دقیق ماه شمسی محاسبه و نمایش می‌دهد.
    """
    check_for_new_shamsi_month(message.from_user.id)
    user_id = message.from_user.id
    j_now = jdatetime.datetime.now()
    start_g, end_g = get_shamsi_month_range(j_now.year, j_now.month)
    
    conn = sqlite3.connect('expenses.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT amount FROM budgets WHERE user_id = ? AND year = ? AND month = ?", (user_id, j_now.year, j_now.month))
    budget_data = cursor.fetchone()

    if not budget_data:
        bot.reply_to(message, "هنوز بودجه‌ای برای این ماه تنظیم نکرده‌ای. با `/setbudget` بساز."); conn.close(); return

    budget_amount = budget_data[0]
    cursor.execute("SELECT SUM(amount) FROM expenses WHERE user_id = ? AND timestamp >= ? AND timestamp < ?", (user_id, start_g, end_g))
    total_spent = cursor.fetchone()[0] or 0
    conn.close()
    
    remaining = budget_amount - total_spent
    percentage = (total_spent / budget_amount) * 100 if budget_amount > 0 else 0
    status_message = (f"💰 *بودجه ماه {j_now.strftime('%B')}:* `{budget_amount:,.0f}` تومان\n"
                      f"🧾 *خرج تا امروز:* `{total_spent:,.0f}` تومان ({percentage:.1f}٪)\n\n"
                      f"{'🟢' if remaining >= 0 else '🔴'} *مانده:* `{remaining:,.0f}` تومان")
    bot.send_message(message.chat.id, status_message, parse_mode='Markdown')

@bot.message_handler(commands=['export'])
def handle_export(message):
    check_for_new_shamsi_month(message.from_user.id)
    user_id = message.from_user.id
    bot.send_message(user_id, "در حال آماده‌سازی خروجی اکسل و نمودار... ⚙️")
    try:
        conn = sqlite3.connect('expenses.db', check_same_thread=False)
        df = pd.read_sql_query("SELECT timestamp, amount, category, note FROM expenses WHERE user_id = ?", conn, params=(user_id,))
        conn.close()
        if df.empty:
            bot.send_message(user_id, "هیچ داده‌ای برای خروجی وجود ندارد."); return
        excel_buffer = BytesIO()
        df.to_excel(excel_buffer, index=False, sheet_name='Expenses'); excel_buffer.seek(0)
        bot.send_document(user_id, ('expenses.xlsx', excel_buffer), caption="خروجی اکسل")
        category_totals = df.groupby('category')['amount'].sum()
        plt.style.use('seaborn-v0_8-pastel')
        fig, ax = plt.subplots(figsize=(10, 8))
        ax.pie(category_totals, labels=category_totals.index, autopct='%1.1f%%', startangle=90); ax.axis('equal')
        plt.title('توزیع هزینه‌ها')
        chart_buffer = BytesIO(); plt.savefig(chart_buffer, format='PNG', bbox_inches='tight'); plt.close(fig)
        chart_buffer.seek(0)
        bot.send_photo(user_id, photo=chart_buffer, caption="نمودار توزیع هزینه‌ها")
    except Exception as e:
        print(f"❌ Export Error: {e}")
        bot.send_message(user_id, "خطا در تولید خروجی.")

@bot.message_handler(commands=['undo'])
def handle_undo(message):
    check_for_new_shamsi_month(message.from_user.id)
    user_id = message.from_user.id
    conn = sqlite3.connect('expenses.db', check_same_thread=False)
    cursor = conn.cursor()
    user_state = get_user_state(cursor, user_id)
    expense_id = user_state.get('last_expense_id')
    if expense_id:
        cursor.execute("SELECT amount, category, note FROM expenses WHERE id = ?", (expense_id,))
        expense_data = cursor.fetchone()
        if expense_data:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✏️ ویرایش", callback_data=f'edit|{expense_id}'),
                       types.InlineKeyboardButton("❌ حذف", callback_data=f'delete|{expense_id}'))
            markup.add(types.InlineKeyboardButton("انصراف", callback_data='cancel_edit'))
            bot.send_message(user_id, f"آخرین هزینه: `{expense_data[0]:,.0f} تومان - {expense_data[1]}`", reply_markup=markup, parse_mode='Markdown')
        else: bot.send_message(user_id, "این هزینه قبلاً حذف شده است.")
    else: bot.send_message(user_id, "هیچ هزینه اخیری برای مدیریت وجود ندارد.")
    conn.close()

@bot.message_handler(commands=['reset'])
def handle_reset(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ بله، مطمئنم", callback_data='reset_confirm|yes'),
               types.InlineKeyboardButton("❌ نه، لغو کن", callback_data='reset_confirm|no'))
    bot.send_message(message.chat.id, "⚠️ **اخطار جدی** ⚠️\nآیا مطمئن هستید که می‌خواهید تمام سوابق مالی را پاک کنید؟ این عمل غیرقابل بازگشت است.", reply_markup=markup, parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def show_stats(message):
    if message.from_user.id != ADMIN_USER_ID:
        bot.send_message(message.chat.id, "⛔ شما اجازه دسترسی به این بخش را ندارید.")
        return
    try:
        conn = sqlite3.connect('expenses.db', check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM expenses")
        total_expenses = cursor.fetchone()[0]
        cursor.execute("SELECT DATE(join_date), COUNT(*) FROM users GROUP BY DATE(join_date) ORDER BY DATE(join_date) DESC LIMIT 5")
        daily_new_users = cursor.fetchall()
        conn.close()

        stats_text = f"📊 **آمار کلی ربات**\n\n👥 **کل کاربران:** {total_users}\n🧾 **کل هزینه‌ها:** {total_expenses}\n\n--- **کاربران جدید روزانه** ---\n"
        for date, count in daily_new_users:
            stats_text += f"🗓️ {date}: {count} کاربر جدید\n"

        bot.send_message(message.chat.id, stats_text, parse_mode='Markdown')
    except Exception as e:
        print(f"❌ Error fetching stats: {e}")
        bot.send_message(message.chat.id, "خطا در دریافت آمار.")

# ------------------ CALLBACKS ------------------
@bot.callback_query_handler(func=lambda call: True)
def handle_callback_query(call):
    user_id = call.from_user.id; chat_id = call.message.chat.id
    parts = call.data.split('|'); action = parts[0]
    if action == 'reset_confirm':
        if parts[1] == 'yes':
            conn = sqlite3.connect('expenses.db', check_same_thread=False)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM expenses WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM budgets WHERE user_id = ?", (user_id,))
            conn.commit(); conn.close()
            bot.edit_message_text("🗑️ تمام سوابق مالی شما پاک شد.", chat_id, call.message.message_id)
        else:
            bot.edit_message_text("👍 عملیات پاک‌سازی لغو شد.", chat_id, call.message.message_id)
    elif action == 'delete':
        expense_id = int(parts[1])
        conn = sqlite3.connect('expenses.db', check_same_thread=False)
        cursor = conn.cursor(); cursor.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id)); conn.commit(); conn.close()
        bot.edit_message_text("✅ هزینه با موفقیت حذف شد.", chat_id, call.message.message_id)
    elif action == 'edit':
        expense_id = int(parts[1])
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("مبلغ", callback_data=f'editfield|amount|{expense_id}'))
        markup.add(types.InlineKeyboardButton("دسته‌بندی", callback_data=f'editfield|category|{expense_id}'))
        markup.add(types.InlineKeyboardButton("توضیحات", callback_data=f'editfield|note|{expense_id}'))
        bot.edit_message_text("کدام بخش را می‌خواهید ویرایش کنید؟", chat_id, call.message.message_id, reply_markup=markup)
    elif action == 'editfield':
        field, expense_id = parts[1], int(parts[2])
        set_user_state(user_id, edit_state={'expense_id': expense_id, 'field': field})
        msg = bot.send_message(user_id, f"لطفاً مقدار جدید برای **{field}** را ارسال کنید:", parse_mode='Markdown')
        bot.register_next_step_handler(msg, process_edit_step)
    elif action == 'cancel_edit':
        bot.edit_message_text("👍 عملیات لغو شد.", chat_id, call.message.message_id)

def process_edit_step(message):
    user_id = message.from_user.id
    conn = sqlite3.connect('expenses.db', check_same_thread=False)
    cursor = conn.cursor()
    state = get_user_state(cursor, user_id).get('edit_state')
    if state:
        expense_id, field = state['expense_id'], state['field']
        new_value = message.text
        allowed_fields = ['amount', 'category', 'note']
        if field in allowed_fields:
            if field == 'amount':
                new_value = normalize_amount(new_value)
                if not new_value or new_value <= 0:
                    bot.send_message(user_id, "مبلغ نامعتبر است."); new_value = None
            if new_value is not None:
                cursor.execute(f"UPDATE expenses SET {field} = ? WHERE id = ? AND user_id = ?", (new_value, expense_id, user_id))
                conn.commit(); bot.send_message(user_id, "✅ هزینه ویرایش شد.")
        else: bot.send_message(user_id, "فیلد نامعتبر است.")
        set_user_state(user_id, edit_state=None)
    conn.close()

# ------------------ TEXT MESSAGE HANDLER ------------------
@bot.message_handler(func=lambda m: True)
def handle_text_message(message):
    if message.text.startswith('/'):
        bot.reply_to(message, "دستور نامشخص است. برای راهنمایی /help را ارسال کنید."); return
    
    check_for_new_shamsi_month(message.from_user.id)
    bot.send_chat_action(message.chat.id, 'typing')
    
    prompt = f'Extract from "{message.text}" into JSON: {{"amount": number, "category": "string", "note": "string"}}. Categories: غذا, حمل و نقل, خرید, تفریح, قبوض, سلامتی, آموزش, هدیه, اجاره, سایر. Example: "۳۲۰۰۰ قهوه" -> {{"amount": 32000, "category": "غذا", "note": "قهوه"}}. Only JSON.'
    ai_response = call_gemini_api(prompt)
    if not ai_response:
        bot.send_message(message.chat.id, "❌ خطا در ارتباط با هوش مصنوعی. لطفاً دوباره تلاش کنید."); return
    try:
        clean_response = ai_response.strip().replace("```json", "").replace("```", "").strip()
        expense_data = json.loads(clean_response)
        amount, category, note = expense_data.get('amount'), expense_data.get('category', 'سایر'), expense_data.get('note', '')
        if amount and isinstance(amount, (int, float)) and amount > 0:
            save_expense(message.from_user.id, amount, category, note)
        else:
            bot.send_message(message.chat.id, "🤔 مبلغ معتبری برای ثبت پیدا نشد. لطفاً در قالب 'مبلغ شرح هزینه' ارسال کنید. مثلا: `35000 ناهار`")
    except json.JSONDecodeError:
        bot.send_message(message.chat.id, f"❌ خطا در تحلیل پاسخ هوش مصنوعی. لطفاً تراکنش را با فرمت دیگری بیان کنید.\nپاسخ دریافت شده:\n`{ai_response}`", parse_mode='Markdown')
    except Exception as e:
        print(f"❌ Error in handle_text_message: {e}")
        bot.send_message(message.chat.id, "خطای پیش‌بینی نشده‌ای رخ داد. لطفاً دوباره تلاش کنید.")

# ------------------ MAIN LOOP ------------------
if __name__ == '__main__':
    print("🚀 Bot starting...")
    init_db()
    bot.infinity_polling(timeout=60, long_polling_timeout=30)