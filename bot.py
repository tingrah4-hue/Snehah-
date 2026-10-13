import os
import sqlite3
import asyncio
import io
import uuid

import qrcode
from khqr import build_khqr
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x}
ADMIN_CONTACT = os.environ.get("ADMIN_CONTACT", "https://t.me/your_admin")
BAKONG_ID = os.environ["BAKONG_ID"]            # e.g. yourname@aba
MERCHANT_NAME = os.environ.get("MERCHANT_NAME", "MY SHOP")
MERCHANT_CITY = os.environ.get("MERCHANT_CITY", "Phnom Penh")

db = sqlite3.connect(os.environ.get("DB_PATH", "shop.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, category_id INT,
    name TEXT, price REAL);
CREATE TABLE IF NOT EXISTS keys(id INTEGER PRIMARY KEY, product_id INT,
    key TEXT, used INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, balance REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, user_id INT, product_id INT,
    qty INT, total REAL, status TEXT, delivered TEXT);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS topups(id TEXT PRIMARY KEY, user_id INT, amount REAL,
    status TEXT);
""")
for _stmt in ("ALTER TABLE categories ADD COLUMN media_id TEXT",
              "ALTER TABLE categories ADD COLUMN media_type TEXT"):
    try:
        db.execute(_stmt)
    except sqlite3.OperationalError:
        pass
db.commit()

DEFAULT_WELCOME = "សូមស្វាគមន៍! ជ្រើសរើសខាងក្រោម 👇"


def get_setting(k, default=""):
    row = db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def set_setting(k, v):
    db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v))
    db.commit()


async def send_media(message, media_type, media_id, caption, kb):
    if media_id and media_type == "video":
        return await message.reply_video(media_id, caption=caption, reply_markup=kb)
    if media_id and media_type == "photo":
        return await message.reply_photo(media_id, caption=caption, reply_markup=kb)
    return await message.reply_text(caption, reply_markup=kb)


def stock(pid):
    return db.execute("SELECT COUNT(*) c FROM keys WHERE product_id=? AND used=0",
                      (pid,)).fetchone()["c"]


def main_menu():
    return Kb([[Btn("🛒 BUY NOW", callback_data="cats")],
               [Btn("💰 Top-up", callback_data="topup"),
                Btn("💳 Balance", callback_data="balance")],
               [Btn("📦 My Orders", callback_data="orders")],
               [Btn("💬 Contact Admin", url=ADMIN_CONTACT)]])


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))
    db.commit()
    await send_media(update.effective_message, get_setting("welcome_media_type"),
                     get_setting("welcome_media_id"),
                     get_setting("welcome_text", DEFAULT_WELCOME), main_menu())


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    parts = q.data.split(":")
    act = parts[0]

    if act in ("cats", "home"):
        rows = db.execute("SELECT * FROM categories").fetchall()
        kb = [[Btn(r["name"], callback_data=f"cat:{r['id']}")] for r in rows]
        kb.append([Btn("🔙 Back", callback_data="menu")])
        await q.message.reply_text("🛒 Choose a category", reply_markup=Kb(kb))

    elif act == "menu":
        await q.message.reply_text("Main menu", reply_markup=main_menu())

    elif act == "cat":
        rows = db.execute("SELECT * FROM products WHERE category_id=?",
                          (parts[1],)).fetchall()
        kb = [[Btn(f"{r['name']} - ${r['price']:.2f} [Stock: {stock(r['id'])}]",
                   callback_data=f"prod:{r['id']}:1")] for r in rows]
        kb.append([Btn("🔙 Back", callback_data="cats")])
        cat = db.execute("SELECT * FROM categories WHERE id=?", (parts[1],)).fetchone()
        await send_media(q.message, cat["media_type"], cat["media_id"],
                         f"{cat['name']}\n\nSelect a product:", Kb(kb))

    elif act == "prod":
        pid, qty = int(parts[1]), int(parts[2])
        p = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        qty = max(1, min(qty, stock(pid) or 1))
        total = p["price"] * qty
        kb = [
            [Btn("➖", callback_data=f"prod:{pid}:{qty-1}"),
             Btn(f"Keys: {qty}", callback_data="noop"),
             Btn("➕", callback_data=f"prod:{pid}:{qty+1}")],
            [Btn("ABA KHQR", callback_data=f"pay:{pid}:{qty}:khqr")],
            [Btn("ABA", callback_data=f"pay:{pid}:{qty}:aba"),
             Btn("Wing", callback_data=f"pay:{pid}:{qty}:wing")],
            [Btn("💰 Buy with Balance", callback_data=f"bal:{pid}:{qty}")],
            [Btn("🔙 Back", callback_data=f"cat:{p['category_id']}")],
        ]
        await q.message.edit_text(
            f"{p['name']}\nQuantity: {qty} | Total: ${total:.2f}",
            reply_markup=Kb(kb))

    elif act == "pay":
        pid, qty, method = int(parts[1]), int(parts[2]), parts[3]
        p = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if stock(pid) < qty:
            await q.message.reply_text("❌ អស់ស្តុក")
            return
        oid = "ORD-" + uuid.uuid4().hex[:8].upper()
        total = p["price"] * qty
        db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?)",
                   (oid, uid, pid, qty, total, "pending", ""))
        db.commit()
        payload = build_khqr(BAKONG_ID, MERCHANT_NAME, MERCHANT_CITY, total, oid)
        img = qrcode.make(payload)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        await q.message.reply_photo(
            buf,
            caption=(f"📋 Order #{oid}\nទំនិញ៖ {p['name']}\n"
                     f"ចំនួនទឹកប្រាក់៖ {total:.2f}ដុល្លារ\n\n"
                     "📲 ស្កេន QR ដោយ ABA ដើម្បីបង់ប្រាក់\n"
                     "បន្ទាប់មកផ្ញើ **រូបថតវិក្កយបត្រ** មកទីនេះ\n"
                     f"(សរសេរ caption: {oid})"),
            parse_mode="Markdown")
        for admin in ADMIN_IDS:
            await ctx.bot.send_message(
                admin, f"New order {oid} ${total:.2f}\n/confirm {oid}")

    elif act == "bal":
        pid, qty = int(parts[1]), int(parts[2])
        p = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        total = p["price"] * qty
        bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
        if bal < total or stock(pid) < qty:
            await q.message.reply_text("❌ សមតុល្យមិនគ្រប់ ឬអស់ស្តុក")
            return
        db.execute("UPDATE users SET balance=balance-? WHERE id=?", (total, uid))
        oid = "ORD-" + uuid.uuid4().hex[:8].upper()
        db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?)",
                   (oid, uid, pid, qty, total, "pending", ""))
        db.commit()
        await deliver(ctx, oid)

    elif act == "balance":
        bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
        await q.message.reply_text(f"💳 សមតុល្យរបស់អ្នក៖ ${bal:.2f}")

    elif act == "topup":
        kb = [[Btn(f"${a}", callback_data=f"topamt:{a}") for a in (1, 2, 5)],
              [Btn(f"${a}", callback_data=f"topamt:{a}") for a in (10, 20, 50)],
              [Btn("🔙 Back", callback_data="menu")]]
        await q.message.reply_text("💰 ជ្រើសចំនួនទឹកប្រាក់ដែលចង់បញ្ចូល", reply_markup=Kb(kb))

    elif act == "topamt":
        amount = float(parts[1])
        if amount not in (1, 2, 5, 10, 20, 50):
            return
        tid = "TOP-" + uuid.uuid4().hex[:8].upper()
        db.execute("INSERT INTO topups VALUES(?,?,?,?)", (tid, uid, amount, "pending"))
        db.commit()
        payload = build_khqr(BAKONG_ID, MERCHANT_NAME, MERCHANT_CITY, amount, tid)
        buf = io.BytesIO()
        qrcode.make(payload).save(buf, format="PNG")
        buf.seek(0)
        await q.message.reply_photo(
            buf,
            caption=(f"💰 Top-up #{tid}\nចំនួនទឹកប្រាក់៖ {amount:.2f}ដុល្លារ\n\n"
                     "📲 ស្កេន QR ដោយ ABA ដើម្បីបង់ប្រាក់\n"
                     "បន្ទាប់មកផ្ញើ **រូបថតវិក្កយបត្រ** មកទីនេះ\n"
                     f"(សរសេរ caption: {tid})"),
            parse_mode="Markdown")

    elif act == "orders":
        rows = db.execute("SELECT * FROM orders WHERE user_id=? ORDER BY rowid DESC LIMIT 10",
                          (uid,)).fetchall()
        text = "\n".join(f"#{r['id']} ${r['total']:.2f} [{r['status']}]" for r in rows)
        await q.message.reply_text(text or "មិនទាន់មានការបញ្ជាទិញ")


async def deliver(ctx, oid):
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] == "completed":
        return False
    ks = db.execute("SELECT * FROM keys WHERE product_id=? AND used=0 LIMIT ?",
                    (o["product_id"], o["qty"])).fetchall()
    if len(ks) < o["qty"]:
        return False
    for k in ks:
        db.execute("UPDATE keys SET used=1 WHERE id=?", (k["id"],))
    keys_text = "\n".join(k["key"] for k in ks)
    db.execute("UPDATE orders SET status='completed', delivered=? WHERE id=?",
               (keys_text, oid))
    db.commit()
    await ctx.bot.send_message(
        o["user_id"],
        f"✅ Payment verified!\nOrder: #{oid}\nAmount Paid: ${o['total']:.2f}\n"
        f"Your Keys:\n{keys_text}\n\nThank you for your purchase!")
    return True


async def on_receipt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Customer sends a payment screenshot; caption should contain the order id."""
    msg = update.message
    caption = msg.caption or ""
    oid = next((w.strip("#") for w in caption.split()
                if w.strip("#").startswith(("ORD-", "TOP-"))), None)
    if not oid:
        for table in ("orders", "topups"):
            row = db.execute(f"SELECT id FROM {table} WHERE user_id=? AND status='pending' "
                             "ORDER BY rowid DESC LIMIT 1", (msg.from_user.id,)).fetchone()
            if row:
                oid = row["id"]
                break
    if not oid:
        await msg.reply_text("មិនឃើញការបញ្ជាទិញ។ សូមចុច /start")
        return
    for admin in ADMIN_IDS:
        await ctx.bot.send_photo(admin, msg.photo[-1].file_id,
                                 caption=f"🧾 Receipt for {oid}\n/confirm {oid}")
    await msg.reply_text("✅ បានទទួលវិក្កយបត្រ សូមរង់ចាំ Admin បញ្ជាក់ (ភាគច្រើនក្នុងពេលខ្លីៗ)")


# ---------- Admin ----------
def is_admin(update):
    return update.effective_user.id in ADMIN_IDS


async def confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not ctx.args:
        return
    oid = ctx.args[0].strip("#")
    if oid.startswith("TOP-"):
        t = db.execute("SELECT * FROM topups WHERE id=?", (oid,)).fetchone()
        if not t or t["status"] == "completed":
            await update.message.reply_text("❌ Not found or already confirmed")
            return
        db.execute("UPDATE topups SET status='completed' WHERE id=?", (oid,))
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (t["amount"], t["user_id"]))
        db.commit()
        await ctx.bot.send_message(
            t["user_id"], f"✅ បញ្ចូលទឹកប្រាក់ ${t['amount']:.2f} ជោគជ័យ!")
        await update.message.reply_text("✅ Balance added")
        return
    ok = await deliver(ctx, oid)
    await update.message.reply_text("✅ Delivered" if ok else "❌ Failed (order/stock)")


async def addcat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/addcat Category name"""
    if not is_admin(update):
        return
    db.execute("INSERT INTO categories(name) VALUES(?)", (" ".join(ctx.args),))
    db.commit()
    await update.message.reply_text("OK")


async def addprod(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/addprod <category_id> <price> <name...>"""
    if not is_admin(update):
        return
    cid, price, *name = ctx.args
    db.execute("INSERT INTO products(category_id,name,price) VALUES(?,?,?)",
               (cid, " ".join(name), float(price)))
    db.commit()
    await update.message.reply_text("OK")


async def addkeys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/addkeys <product_id> key1 key2 key3 ..."""
    if not is_admin(update):
        return
    pid, *ks = ctx.args
    db.executemany("INSERT INTO keys(product_id,key) VALUES(?,?)",
                   [(pid, k) for k in ks])
    db.commit()
    await update.message.reply_text(f"Added {len(ks)} keys")


async def addbal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/addbal <user_id> <amount>"""
    if not is_admin(update):
        return
    uid, amt = ctx.args
    db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (float(amt), uid))
    db.commit()
    await update.message.reply_text("OK")


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: send photo/video with caption '/setmedia welcome' or '/setmedia cat <id>'.
    Anyone else: treated as a payment receipt (photos only)."""
    msg = update.message
    caption = (msg.caption or "").strip()
    if caption.startswith("/setmedia"):
        if not is_admin(update):
            return
        if msg.video:
            mtype, fid = "video", msg.video.file_id
        else:
            mtype, fid = "photo", msg.photo[-1].file_id
        args = caption.split()[1:]
        if args[:1] == ["welcome"]:
            set_setting("welcome_media_type", mtype)
            set_setting("welcome_media_id", fid)
        elif len(args) == 2 and args[0] == "cat":
            db.execute("UPDATE categories SET media_type=?, media_id=? WHERE id=?",
                       (mtype, fid, args[1]))
            db.commit()
        else:
            await msg.reply_text("Usage: caption '/setmedia welcome' or '/setmedia cat <id>'")
            return
        await msg.reply_text("OK")
        return
    if msg.photo:
        await on_receipt(update, ctx)


async def setwelcome(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/setwelcome <text...>"""
    if not is_admin(update) or not ctx.args:
        return
    set_setting("welcome_text", update.message.text.split(" ", 1)[1])
    await update.message.reply_text("OK")


async def setprice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/setprice <product_id> <price>"""
    if not is_admin(update) or len(ctx.args) != 2:
        return
    db.execute("UPDATE products SET price=? WHERE id=?", (float(ctx.args[1]), ctx.args[0]))
    db.commit()
    await update.message.reply_text("OK")


async def delprod(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/delprod <product_id>  (also deletes its unused keys)"""
    if not is_admin(update) or not ctx.args:
        return
    db.execute("DELETE FROM keys WHERE product_id=? AND used=0", (ctx.args[0],))
    db.execute("DELETE FROM products WHERE id=?", (ctx.args[0],))
    db.commit()
    await update.message.reply_text("OK")


async def delcat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/delcat <category_id>  (also deletes its products and unused keys)"""
    if not is_admin(update) or not ctx.args:
        return
    cid = ctx.args[0]
    db.execute("DELETE FROM keys WHERE used=0 AND product_id IN "
               "(SELECT id FROM products WHERE category_id=?)", (cid,))
    db.execute("DELETE FROM products WHERE category_id=?", (cid,))
    db.execute("DELETE FROM categories WHERE id=?", (cid,))
    db.commit()
    await update.message.reply_text("OK")


async def list_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/list : show category and product IDs with price and stock"""
    if not is_admin(update):
        return
    lines = []
    for c in db.execute("SELECT * FROM categories").fetchall():
        lines.append(f"📁 [{c['id']}] {c['name']}")
        for p in db.execute("SELECT * FROM products WHERE category_id=?", (c["id"],)):
            lines.append(f"   • [{p['id']}] {p['name']} ${p['price']:.2f} (stock {stock(p['id'])})")
    await update.message.reply_text("\n".join(lines) or "Empty")


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/stats : sales summary"""
    if not is_admin(update):
        return
    users = db.execute("SELECT COUNT(*) c, COALESCE(SUM(balance),0) b FROM users").fetchone()
    done = db.execute("SELECT COUNT(*) c, COALESCE(SUM(total),0) t FROM orders "
                      "WHERE status='completed'").fetchone()
    pending = db.execute("SELECT COUNT(*) c FROM orders WHERE status='pending'").fetchone()["c"]
    lines = [f"👥 Users: {users['c']} (balances ${users['b']:.2f})",
             f"✅ Completed orders: {done['c']} | Revenue: ${done['t']:.2f}",
             f"⏳ Pending orders: {pending}", "", "📦 Sold / In stock:"]
    for p in db.execute("SELECT * FROM products").fetchall():
        sold = db.execute("SELECT COALESCE(SUM(qty),0) s FROM orders WHERE product_id=? "
                          "AND status='completed'", (p["id"],)).fetchone()["s"]
        left = stock(p["id"])
        warn = " ⚠️ low" if left < 5 else ""
        lines.append(f"[{p['id']}] {p['name']}: sold {sold} / stock {left}{warn}")
    await update.message.reply_text("\n".join(lines))


async def broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/broadcast <text...> : message all users"""
    if not is_admin(update) or not ctx.args:
        return
    text = update.message.text.split(" ", 1)[1]
    ok = 0
    for u in db.execute("SELECT id FROM users").fetchall():
        try:
            await ctx.bot.send_message(u["id"], text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"Sent to {ok} users")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("my_order", lambda u, c: start(u, c)))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("addcat", addcat))
    app.add_handler(CommandHandler("addprod", addprod))
    app.add_handler(CommandHandler("addkeys", addkeys))
    app.add_handler(CommandHandler("addbal", addbal))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, on_media))
    app.add_handler(CommandHandler("setwelcome", setwelcome))
    app.add_handler(CommandHandler("setprice", setprice))
    app.add_handler(CommandHandler("delprod", delprod))
    app.add_handler(CommandHandler("delcat", delcat))
    app.add_handler(CommandHandler("list", list_all))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.run_polling()


if __name__ == "__main__":
    main()
