"""
Telegram 文件夹式视频管理机器人（最终稳定版）
命令：
    /start /help       帮助
    /newfolder        创建文件夹
    /folders          查看文件夹
    /open 名          选择文件夹
    /get 名           获取视频
    /list 名          视频列表
    /delVideo 名 编号  删除视频
    /rename 旧 新      重命名
    /delFolder 名     删除文件夹
"""

import logging
import os
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)
import asyncpg

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ASK_FOLDER_NAME = 1

async def get_db():
    return await asyncpg.connect(DATABASE_URL)

async def init_db():
    conn = await get_db()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id         SERIAL PRIMARY KEY,
                owner_id   BIGINT NOT NULL,
                name       TEXT   NOT NULL,
                UNIQUE(owner_id, name)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id           SERIAL PRIMARY KEY,
                folder_id    INT  REFERENCES folders(id) ON DELETE CASCADE,
                file_id      TEXT NOT NULL,
                file_name    TEXT,
                caption      TEXT,
                forward_from TEXT
            )
        """)
        logger.info("✅ 数据库就绪")
    finally:
        await conn.close()

async def db_create_folder(owner_id: int, name: str):
    conn = await get_db()
    try:
        await conn.execute("INSERT INTO folders (owner_id, name) VALUES ($1,$2)", owner_id, name)
        return True
    except asyncpg.UniqueViolationError:
        return False
    finally:
        await conn.close()

async def db_list_folders(owner_id: int):
    conn = await get_db()
    try:
        return await conn.fetch("""
            SELECT f.id,f.name,COUNT(v.id) AS c
            FROM folders f LEFT JOIN videos v ON v.folder_id=f.id
            WHERE f.owner_id=$1 GROUP BY f.id,f.name ORDER BY f.name
        """, owner_id)
    finally:
        await conn.close()

async def db_get_folder(owner_id: int, name: str):
    conn = await get_db()
    try:
        return await conn.fetchrow("SELECT * FROM folders WHERE owner_id=$1 AND name=$2", owner_id, name)
    finally:
        await conn.close()

async def db_rename_folder(owner_id: int, o: str, n: str):
    conn = await get_db()
    try:
        row = await conn.fetchrow("SELECT id FROM folders WHERE owner_id=$1 AND name=$2", owner_id, o)
        if not row: return "nf"
        try:
            await conn.execute("UPDATE folders SET name=$1 WHERE id=$2", n, row["id"])
            return "ok"
        except: return "dup"
    finally:
        await conn.close()

async def db_delete_folder(owner_id: int, name: str):
    conn = await get_db()
    try:
        return await conn.execute("DELETE FROM folders WHERE owner_id=$1 AND name=$2", owner_id, name) == "DELETE 1"
    finally:
        await conn.close()

async def db_add_video(fid: int, file_id: str, fname: str, cap: str, src: str):
    conn = await get_db()
    try:
        await conn.execute("""
            INSERT INTO videos (folder_id,file_id,file_name,caption,forward_from)
            VALUES ($1,$2,$3,$4,$5)
        """, fid, file_id, fname, cap, src)
    finally:
        await conn.close()

async def db_get_videos(fid: int):
    conn = await get_db()
    try:
        return await conn.fetch("SELECT * FROM videos WHERE folder_id=$1", fid)
    finally:
        await conn.close()

async def db_del_video(fid: int, idx: int):
    conn = await get_db()
    try:
        vs = await conn.fetch("SELECT id FROM videos WHERE folder_id=$1", fid)
        if 1 <= idx <= len(vs):
            await conn.execute("DELETE FROM videos WHERE id=$1", vs[idx-1]["id"])
            return True
        return False
    finally:
        await conn.close()

def get_forward_source(msg):
    if msg.forward_origin:
        o = msg.forward_origin
        if o.type == "channel": return o.chat.title
        if o.type == "user": return f"{o.sender_user.first_name}"
        if o.type == "hidden_user": return o.sender_user_name
    return None

async def kb(uid: int, pre: str):
    rows = await db_list_folders(uid)
    if not rows: return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"📁 {r['name']} ({r['c']})", callback_data=f"{pre}:{r['name']}")
    ]] for r in rows)

async def help(update: Update, c: ContextTypes):
    await update.message.reply_text("""
📁 视频文件夹机器人

/newfolder - 创建文件夹
/folders   - 查看所有
/open 名   - 选定文件夹
/get 名    - 获取视频
/list 名   - 视频列表
/delVideo 名 编号 - 删除视频
/rename 旧 新 - 改名
/delFolder 名 - 删除文件夹
""")

async def newfolder(update: Update, c: ContextTypes):
    await update.message.reply_text("📁 输入文件夹名称：")
    return ASK_FOLDER_NAME

async def recv_name(update: Update, c: ContextTypes):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ 名称不能为空")
        return ASK_FOLDER_NAME
    ok = await db_create_folder(update.effective_user.id, name)
    if ok:
        await update.message.reply_text(f"✅ 创建：{name}")
    else:
        await update.message.reply_text("⚠️ 已存在")
        return ASK_FOLDER_NAME
    return ConversationHandler.END

async def cancel(update: Update, c: ContextTypes):
    await update.message.reply_text("✅ 已取消")
    return ConversationHandler.END

async def folders(update: Update, c: ContextTypes):
    rows = await db_list_folders(update.effective_user.id)
    if not rows:
        await update.message.reply_text("📂 暂无文件夹")
        return
    txt = "\n".join(f"📁 {r['name']} — {r['c']} 个视频" for r in rows)
    await update.message.reply_text("📋 你的文件夹\n\n" + txt)

async def open_cmd(update: Update, c: ContextTypes):
    uid = update.effective_user.id
    if not c.args:
        k = await kb(uid, "open")
        if not k: await update.message.reply_text("先创建文件夹"); return
        await update.message.reply_text("📂 选择：", reply_markup=k)
        return
    name = " ".join(c.args)
    f = await db_get_folder(uid, name)
    if f:
        c.user_data["cur"] = name
        await update.message.reply_text(f"✅ 已打开：{name}")
    else:
        await update.message.reply_text("❌ 不存在")

async def cb_open(u: Update, c: ContextTypes):
    q = u.callback_query
    await q.answer()
    c.user_data["cur"] = q.data.split(":")[1]
    await q.edit_message_text(f"✅ 已打开：{q.data.split(':')[1]}")

async def list_cmd(update: Update, c: ContextTypes):
    if not c.args:
        await update.message.reply_text("用法：/list 文件夹名")
        return
    name = " ".join(c.args)
    f = await db_get_folder(update.effective_user.id, name)
    if not f:
        await update.message.reply_text("❌ 不存在")
        return
    vs = await db_get_videos(f["id"])
    if not vs:
        await update.message.reply_text("📂 空")
        return
    txt = f"📜 {name}\n\n" + "\n".join(f"{i}. 视频{i}" for i,v in enumerate(vs,1))
    await update.message.reply_text(txt)

async def delvideo(update: Update, c: ContextTypes):
    if len(c.args)<2:
        await update.message.reply_text("用法：/delVideo 文件夹 编号")
        return
    try:
        name, idx = c.args[0], int(c.args[1])
    except:
        await update.message.reply_text("❌ 编号必须是数字")
        return
    f = await db_get_folder(update.effective_user.id, name)
    if not f:
        await update.message.reply_text("❌ 不存在")
        return
    ok = await db_del_video(f["id"], idx)
    await update.message.reply_text("🗑 已删除" if ok else "❌ 失败")

async def get_cmd(update: Update, c: ContextTypes):
    uid = update.effective_user.id
    if not c.args:
        k = await kb(uid, "get")
        if not k: await update.message.reply_text("先创建文件夹"); return
        await update.message.reply_text("📤 获取：", reply_markup=k)
        return
    name = " ".join(c.args)
    await send_videos(update.message, uid, name)

async def cb_get(u: Update, c: ContextTypes):
    q = u.callback_query
    await q.answer()
    await q.edit_message_text(f"📤 发送：{q.data.split(':')[1]}")
    await send_videos(q.message, q.from_user.id, q.data.split(':')[1])

async def send_videos(msg, uid, name):
    f = await db_get_folder(uid, name)
    if not f:
        await msg.reply_text("❌ 不存在")
        return
    vs = await db_get_videos(f["id"])
    if not vs:
        await msg.reply_text("📂 空")
        return
    for v in vs:
        try:
            await msg.reply_video(v["file_id"], caption=f"来自：{name}")
        except:
            pass

async def rename(update: Update, c: ContextTypes):
    if len(c.args)<2:
        await update.message.reply_text("用法：/rename 旧名 新名")
        return
    r = await db_rename_folder(update.effective_user.id, c.args[0], " ".join(c.args[1:]))
    if r=="ok": await update.message.reply_text("✅ 成功")
    elif r=="nf": await update.message.reply_text("❌ 不存在")
    else: await update.message.reply_text("⚠️ 重名")

async def delfolder(update: Update, c: ContextTypes):
    if not c.args:
        await update.message.reply_text("用法：/delFolder 名称")
        return
    name = " ".join(c.args)
    await update.message.reply_text(f"⚠️ 删除 {name}？", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 确认", callback_data=f"delc:{name}")],
        [InlineKeyboardButton("❌ 取消", callback_data="delca")]
    ]))

async def cb_delc(u: Update, c: ContextTypes):
    q = u.callback_query
    await q.answer()
    ok = await db_delete_folder(q.from_user.id, q.data.split(":")[1])
    await q.edit_message_text("🗑 已删除" if ok else "❌ 失败")

async def cb_delca(u: Update, c: ContextTypes):
    await u.callback_query.answer()
    await u.callback_query.edit_message_text("✅ 已取消")

async def video(u: Update, c: ContextTypes):
    msg = u.effective_message
    v = msg.video or msg.document
    if not v: return
    uid = u.effective_user.id
    cur = c.user_data.get("cur")
    if cur:
        f = await db_get_folder(uid, cur)
        if f:
            await db_add_video(f["id"], v.file_id, v.file_name or "", msg.caption or "", get_forward_source(msg))
            await msg.reply_text(f"✅ 已存入：{cur}")
            return
    k = await kb(uid, "save")
    if not k:
        await msg.reply_text("先创建文件夹")
        return
    c.user_data["pv"] = {"fid":v.file_id,"fn":v.file_name,"cap":msg.caption,"src":get_forward_source(msg)}
    await msg.reply_text("📂 选择位置：", reply_markup=k)

async def cb_save(u: Update, c: ContextTypes):
    q = u.callback_query
    await q.answer()
    pv = c.user_data.pop("pv", None)
    if not pv:
        await q.edit_message_text("⚠️ 超时")
        return
    f = await db_get_folder(q.from_user.id, q.data.split(":")[1])
    await db_add_video(f["id"], pv["fid"], pv["fn"] or "", pv["cap"] or "", pv["src"])
    await q.edit_message_text(f"✅ 已存入：{q.data.split(':')[1]}")

async def post_init(app):
    await init_db()

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("newfolder", newfolder)],
        states={ASK_FOLDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_name)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler(["start","help"], help))
    app.add_handler(CommandHandler("folders", folders))
    app.add_handler(CommandHandler("open", open_cmd))
    app.add_handler(CommandHandler("get", get_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("delVideo", delvideo))
    app.add_handler(CommandHandler("rename", rename))
    app.add_handler(CommandHandler("delFolder", delfolder))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video))

    app.add_handler(CallbackQueryHandler(cb_open, pattern="^open:"))
    app.add_handler(CallbackQueryHandler(cb_get, pattern="^get:"))
    app.add_handler(CallbackQueryHandler(cb_save, pattern="^save:"))
    app.add_handler(CallbackQueryHandler(cb_delc, pattern="^delc:"))
    app.add_handler(CallbackQueryHandler(cb_delca, pattern="^delca$"))

    asyncio.run(app.run_polling())

if __name__ == "__main__":
    main()
