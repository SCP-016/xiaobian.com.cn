"""
Telegram 文件夹式视频管理机器人（优化版 + 删除视频功能）
================================
命令：
    /start / /help     使用说明
    /newfolder         创建新文件夹
    /folders           查看所有文件夹
    /open <文件夹名>   选择当前文件夹
    /get  <文件夹名>   取出该文件夹所有视频
    /list <文件夹名>   查看文件夹内视频列表（带编号）
    /delVideo <文件夹名> <视频编号>  删除单个视频
    /rename <旧名> <新名>  重命名文件夹
    /delFolder <文件夹名>  删除文件夹

视频仅保存 file_id，不占用服务器存储空间！
"""

import logging
import os
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

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

ASK_FOLDER_NAME = 1


# ══════════════════════════════════════════
#  数据库
# ══════════════════════════════════════════
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
                created_at TIMESTAMPTZ DEFAULT NOW(),
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
                forward_from TEXT,
                added_at     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        logger.info("✅ 数据库初始化完成")
    finally:
        await conn.close()


async def db_create_folder(owner_id: int, name: str) -> bool:
    conn = await get_db()
    try:
        await conn.execute(
            "INSERT INTO folders (owner_id, name) VALUES ($1, $2)",
            owner_id, name,
        )
        return True
    except asyncpg.UniqueViolationError:
        return False
    finally:
        await conn.close()


async def db_list_folders(owner_id: int):
    conn = await get_db()
    try:
        return await conn.fetch(
            """
            SELECT f.id, f.name, COUNT(v.id) AS video_count
            FROM folders f
            LEFT JOIN videos v ON v.folder_id = f.id
            WHERE f.owner_id = $1
            GROUP BY f.id, f.name
            ORDER BY f.name
            """,
            owner_id,
        )
    finally:
        await conn.close()


async def db_get_folder(owner_id: int, name: str):
    conn = await get_db()
    try:
        return await conn.fetchrow(
            "SELECT * FROM folders WHERE owner_id=$1 AND name=$2",
            owner_id, name,
        )
    finally:
        await conn.close()


async def db_rename_folder(owner_id: int, old_name: str, new_name: str) -> str:
    conn = await get_db()
    try:
        row = await conn.fetchrow(
            "SELECT id FROM folders WHERE owner_id=$1 AND name=$2",
            owner_id, old_name,
        )
        if not row:
            return "not_found"
        try:
            await conn.execute(
                "UPDATE folders SET name=$1 WHERE id=$2",
                new_name, row["id"],
            )
            return "ok"
        except asyncpg.UniqueViolationError:
            return "duplicate"
    finally:
        await conn.close()


async def db_delete_folder(owner_id: int, name: str) -> bool:
    conn = await get_db()
    try:
        result = await conn.execute(
            "DELETE FROM folders WHERE owner_id=$1 AND name=$2",
            owner_id, name,
        )
        return result == "DELETE 1"
    finally:
        await conn.close()


async def db_add_video(folder_id: int, file_id: str, file_name: str,
                       caption: str, forward_from: str):
    conn = await get_db()
    try:
        await conn.execute(
            """
            INSERT INTO videos (folder_id, file_id, file_name, caption, forward_from)
            VALUES ($1, $2, $3, $4, $5)
            """,
            folder_id, file_id, file_name, caption, forward_from,
        )
    finally:
        await conn.close()


async def db_get_videos(folder_id: int):
    conn = await get_db()
    try:
        return await conn.fetch(
            "SELECT * FROM videos WHERE folder_id=$1 ORDER BY added_at",
            folder_id,
        )
    finally:
        await conn.close()


# ══════════════════════════════════════════
# 【新增】删除单个视频
# ══════════════════════════════════════════
async def db_delete_video(folder_id: int, video_index: int):
    conn = await get_db()
    try:
        videos = await conn.fetch(
            "SELECT id FROM videos WHERE folder_id=$1 ORDER BY added_at",
            folder_id,
        )
        if 1 <= video_index <= len(videos):
            video_id = videos[video_index - 1]["id"]
            await conn.execute("DELETE FROM videos WHERE id=$1", video_id)
            return True
        return False
    finally:
        await conn.close()


# ══════════════════════════════════════════
# 工具
# ══════════════════════════════════════════
def get_forward_source(message) -> str | None:
    if message.forward_origin:
        origin = message.forward_origin
        t = getattr(origin, "type", None)
        if t == "channel":
            chat = getattr(origin, "chat", None)
            return getattr(chat, "title", None) or getattr(chat, "username", None)
        elif t == "user":
            u = getattr(origin, "sender_user", None)
            if u:
                return f"{u.first_name} {u.last_name}".strip()
        elif t == "hidden_user":
            return getattr(origin, "sender_user_name", "匿名用户")
        elif t == "chat":
            chat = getattr(origin, "sender_chat", None)
            return getattr(chat, "title", None) or getattr(chat, "username", None)
    if message.forward_from:
        u = message.forward_from
        return f"{u.first_name} {u.last_name}".strip()
    if message.forward_from_chat:
        return message.forward_from_chat.title
    return None


async def build_folder_keyboard(owner_id: int, callback_prefix: str):
    rows = await db_list_folders(owner_id)
    if not rows:
        return None
    buttons = []
    for r in rows:
        buttons.append([
            InlineKeyboardButton(
                f"📁 {r['name']} ({r['video_count']}个)",
                callback_data=f"{callback_prefix}:{r['name']}"
            )
        ])
    return InlineKeyboardMarkup(buttons)


# ══════════════════════════════════════════
# 帮助
# ══════════════════════════════════════════
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📁 *视频文件夹机器人（优化版）*\n\n"
        "🆕 `/newfolder` — 创建文件夹\n"
        "📋 `/folders` — 查看所有文件夹\n"
        "📂 `/open 名称` — 选定文件夹（自动存入）\n"
        "📜 `/list 名称` — 查看视频列表\n"
        "📤 `/get 名称` — 获取所有视频\n"
        "🗑 `/delVideo 文件夹 编号` — 删除单个视频\n"
        "✏️ `/rename 旧名 新名` — 重命名\n"
        "🗑 `/delFolder 名称` — 删除文件夹\n",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════
# 新建文件夹
# ══════════════════════════════════════════
async def cmd_newfolder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📁 输入文件夹名称：")
    return ASK_FOLDER_NAME

async def receive_folder_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ 名称不能为空")
        return ASK_FOLDER_NAME
    ok = await db_create_folder(update.effective_user.id, name)
    if ok:
        await update.message.reply_text(f"✅ 创建成功：{name}")
    else:
        await update.message.reply_text("⚠️ 已存在同名文件夹")
        return ASK_FOLDER_NAME
    return ConversationHandler.END

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ 已取消")
    return ConversationHandler.END


# ══════════════════════════════════════════
# 查看文件夹
# ══════════════════════════════════════════
async def cmd_folders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await db_list_folders(update.effective_user.id)
    if not rows:
        await update.message.reply_text("📂 暂无文件夹")
        return
    msg = "📋 *你的文件夹*\n\n"
    for r in rows:
        msg += f"📁 {r['name']} — {r['video_count']} 个视频\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ══════════════════════════════════════════
# 打开文件夹
# ══════════════════════════════════════════
async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        kb = await build_folder_keyboard(uid, "open")
        await update.message.reply_text("📂 选择文件夹：", reply_markup=kb)
        return
    name = " ".join(context.args)
    f = await db_get_folder(uid, name)
    if f:
        context.user_data["current_folder"] = name
        await update.message.reply_text(f"✅ 已打开：{name}")
    else:
        await update.message.reply_text("❌ 不存在")

async def cb_open_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    name = q.data.split(":")[1]
    context.user_data["current_folder"] = name
    await q.edit_message_text(f"✅ 已打开：{name}")


# ══════════════════════════════════════════
# 【新增】查看视频列表
# ══════════════════════════════════════════
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("用法：/list 文件夹名")
        return
    name = " ".join(context.args)
    f = await db_get_folder(uid, name)
    if not f:
        await update.message.reply_text("❌ 文件夹不存在")
        return
    vs = await db_get_videos(f["id"])
    if not vs:
        await update.message.reply_text("📂 空文件夹")
        return
    txt = f"📜 *{name}* 内视频：\n\n"
    for i, v in enumerate(vs, 1):
        txt += f"{i}. 视频 {i}\n"
    await update.message.reply_text(txt, parse_mode="Markdown")


# ══════════════════════════════════════════
# 【新增】删除单个视频
# ══════════════════════════════════════════
async def cmd_del_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text("用法：/delVideo 文件夹名 视频编号")
        return
    fname = context.args[0]
    try:
        idx = int(context.args[1])
    except:
        await update.message.reply_text("❌ 编号必须是数字")
        return
    f = await db_get_folder(uid, fname)
    if not f:
        await update.message.reply_text("❌ 文件夹不存在")
        return
    ok = await db_delete_video(f["id"], idx)
    if ok:
        await update.message.reply_text(f"🗑 已删除视频 {idx}")
    else:
        await update.message.reply_text("❌ 删除失败（编号错误）")


# ══════════════════════════════════════════
# 获取视频
# ══════════════════════════════════════════
async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        kb = await build_folder_keyboard(uid, "get")
        await update.message.reply_text("📤 获取视频：", reply_markup=kb)
        return
    name = " ".join(context.args)
    await send_videos(update.message, uid, name)

async def cb_get_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    name = q.data.split(":")[1]
    await q.edit_message_text(f"📤 正在发送：{name}")
    await send_videos(q.message, q.from_user.id, name)

async def send_videos(msg, uid, fname):
    f = await db_get_folder(uid, fname)
    if not f:
        await msg.reply_text("❌ 不存在")
        return
    vs = await db_get_videos(f["id"])
    if not vs:
        await msg.reply_text("📂 空文件夹")
        return
    for v in vs:
        try:
            await msg.reply_video(v["file_id"], caption=f"来自：{fname}")
        except:
            continue


# ══════════════════════════════════════════
# 重命名 & 删除文件夹
# ══════════════════════════════════════════
async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("用法：/rename 旧名 新名")
        return
    res = await db_rename_folder(update.effective_user.id, context.args[0], " ".join(context.args[1:]))
    if res == "ok":
        await update.message.reply_text("✅ 已重命名")
    elif res == "not_found":
        await update.message.reply_text("❌ 不存在")
    else:
        await update.message.reply_text("⚠️ 新名称已存在")

async def cmd_delFolder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/delFolder 名称")
        return
    name = " ".join(context.args)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 确认删除", callback_data=f"delconfirm:{name}")],
        [InlineKeyboardButton("❌ 取消", callback_data="delcancel")]
    ])
    await update.message.reply_text(f"⚠️ 删除 {name}？", reply_markup=kb)

async def cb_del_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    name = q.data.split(":")[1]
    ok = await db_delete_folder(q.from_user.id, name)
    await q.edit_message_text("🗑 已删除" if ok else "❌ 失败")

async def cb_del_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("✅ 已取消")


# ══════════════════════════════════════════
# 接收视频
# ══════════════════════════════════════════
async def receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    video = msg.video or msg.document
    if not video:
        return
    uid = update.effective_user.id
    current = context.user_data.get("current_folder")
    if current:
        f = await db_get_folder(uid, current)
        if f:
            await db_add_video(f["id"], video.file_id, video.file_name or "", msg.caption or "", get_forward_source(msg))
            await msg.reply_text(f"✅ 已存入：{current}")
            return
    kb = await build_folder_keyboard(uid, "saveto")
    context.user_data["pv"] = {"fid": video.file_id, "name": video.file_name, "cap": msg.caption, "src": get_forward_source(msg)}
    await msg.reply_text("📂 选择保存位置：", reply_markup=kb)

async def cb_save_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    name = q.data.split(":")[1]
    pv = context.user_data.pop("pv", None)
    if not pv:
        await q.edit_message_text("⚠️ 超时，请重发")
        return
    f = await db_get_folder(q.from_user.id, name)
    await db_add_video(f["id"], pv["fid"], pv["name"] or "", pv["cap"] or "", pv["src"])
    await q.edit_message_text(f"✅ 已存入：{name}")


# ══════════════════════════════════════════
# 启动
# ══════════════════════════════════════════
async def post_init(app):
    await init_db()

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("newfolder", cmd_newfolder)],
        states={ASK_FOLDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_folder_name)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("folders", cmd_folders))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("delVideo", cmd_del_video))
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(CommandHandler("delFolder", cmd_delFolder))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, receive_video))

    app.add_handler(CallbackQueryHandler(cb_open_folder, pattern="^open:"))
    app.add_handler(CallbackQueryHandler(cb_get_folder, pattern="^get:"))
    app.add_handler(CallbackQueryHandler(cb_save_to, pattern="^saveto:"))
    app.add_handler(CallbackQueryHandler(cb_del_confirm, pattern="^delconfirm:"))
    app.add_handler(CallbackQueryHandler(cb_del_cancel, pattern="^delcancel$"))

     import asyncio
    asyncio.run(app.run_polling())

if __name__ == "__main__":
    main()
