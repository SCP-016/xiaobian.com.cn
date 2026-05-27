"""
Telegram 文件夹式视频管理机器人
================================
命令：
    /start / /help     使用说明
    /newfolder         创建新文件夹（Bot 会询问名称）
    /folders           查看所有文件夹
    /open <文件夹名>   选择当前文件夹，之后转发的视频自动存入
    /get  <文件夹名>   取出该文件夹内所有视频
    /rename <旧名> <新名>  重命名文件夹
    /delFolder <文件夹名>  删除文件夹及其所有视频

    转发视频时：
        • 若已用 /open 选择了文件夹 → 直接存入
        • 若未选择 → Bot 显示文件夹列表供你选择

环境变量（Railway Variables）：
    BOT_TOKEN       Telegram Bot Token（必填）
    DATABASE_URL    Railway PostgreSQL 连接串（必填，自动注入）
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

BOT_TOKEN   = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

# ConversationHandler 状态
ASK_FOLDER_NAME = 1
ASK_WHICH_FOLDER = 2


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
        logger.info("数据库初始化完成")
    finally:
        await conn.close()


# ── 文件夹 CRUD ──

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
            SELECT f.id, f.name,
                   COUNT(v.id) AS video_count
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


# ── 视频 CRUD ──

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
#  工具函数
# ══════════════════════════════════════════

def get_forward_source(message) -> str | None:
    """提取转发来源名称"""
    if message.forward_origin:
        origin = message.forward_origin
        t = getattr(origin, "type", None)
        if t == "channel":
            chat = getattr(origin, "chat", None)
            return getattr(chat, "title", None) or getattr(chat, "username", None)
        elif t == "user":
            u = getattr(origin, "sender_user", None)
            if u:
                return f"{u.first_name or ''} {u.last_name or ''}".strip()
        elif t == "hidden_user":
            return getattr(origin, "sender_user_name", "匿名用户")
        elif t == "chat":
            chat = getattr(origin, "sender_chat", None)
            return getattr(chat, "title", None) or getattr(chat, "username", None)
    if message.forward_from:
        u = message.forward_from
        return f"{u.first_name or ''} {u.last_name or ''}".strip()
    if message.forward_from_chat:
        return message.forward_from_chat.title or message.forward_from_chat.username
    return None


async def build_folder_keyboard(owner_id: int, callback_prefix: str):
    """生成文件夹选择按钮"""
    rows = await db_list_folders(owner_id)
    if not rows:
        return None
    buttons = [
        [InlineKeyboardButton(
            f"📁 {r['name']}  ({r['video_count']} 个视频)",
            callback_data=f"{callback_prefix}:{r['name']}"
        )]
        for r in rows
    ]
    return InlineKeyboardMarkup(buttons)


# ══════════════════════════════════════════
#  /start  /help
# ══════════════════════════════════════════

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📁 *视频文件夹机器人*\n\n"
        "🆕 `/newfolder` — 创建新文件夹\n"
        "📋 `/folders` — 查看所有文件夹\n"
        "📂 `/open 文件夹名` — 选择当前文件夹\n"
        "   之后转发的视频自动存入该文件夹\n"
        "📤 `/get 文件夹名` — 取出文件夹内所有视频\n"
        "✏️ `/rename 旧名 新名` — 重命名文件夹\n"
        "🗑 `/delFolder 文件夹名` — 删除文件夹及视频\n\n"
        "💡 *转发视频时*：\n"
        "• 若已 `/open` 选了文件夹 → 直接存入\n"
        "• 若未选 → Bot 会弹出文件夹列表让你选",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════
#  /newfolder — 创建文件夹（对话流程）
# ══════════════════════════════════════════

async def cmd_newfolder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📁 请输入新文件夹的名称：\n"
        "（发送 /cancel 取消）"
    )
    return ASK_FOLDER_NAME


async def receive_folder_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("名称不能为空，请重新输入：")
        return ASK_FOLDER_NAME

    owner_id = update.effective_user.id
    ok = await db_create_folder(owner_id, name)
    if ok:
        await update.message.reply_text(
            f"✅ 文件夹 *{name}* 已创建！\n\n"
            f"用 `/open {name}` 选择它，之后转发视频会自动存入。",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"⚠️ 文件夹 *{name}* 已存在，请换个名称：",
            parse_mode="Markdown",
        )
        return ASK_FOLDER_NAME

    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("已取消。")
    return ConversationHandler.END


# ══════════════════════════════════════════
#  /folders — 列出所有文件夹
# ══════════════════════════════════════════

async def cmd_folders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    rows = await db_list_folders(owner_id)
    if not rows:
        await update.message.reply_text(
            "还没有文件夹，用 `/newfolder` 创建一个吧！",
            parse_mode="Markdown",
        )
        return

    current = context.user_data.get("current_folder")
    lines = []
    for r in rows:
        marker = " ◀ 当前" if r["name"] == current else ""
        lines.append(f"📁 *{r['name']}*  —  {r['video_count']} 个视频{marker}")

    await update.message.reply_text(
        "📋 *你的文件夹：*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════
#  /open — 选择当前文件夹
# ══════════════════════════════════════════

async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    args = context.args

    if not args:
        # 没传名称 → 弹出按钮列表
        keyboard = await build_folder_keyboard(owner_id, "open")
        if not keyboard:
            await update.message.reply_text(
                "还没有文件夹，用 `/newfolder` 先创建一个。",
                parse_mode="Markdown",
            )
            return
        await update.message.reply_text("请选择要打开的文件夹：", reply_markup=keyboard)
        return

    name = " ".join(args)
    folder = await db_get_folder(owner_id, name)
    if not folder:
        await update.message.reply_text(f"❌ 找不到文件夹 *{name}*", parse_mode="Markdown")
        return

    context.user_data["current_folder"] = name
    await update.message.reply_text(
        f"📂 已选择文件夹 *{name}*\n现在转发视频会自动存入这里。",
        parse_mode="Markdown",
    )


async def cb_open_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    context.user_data["current_folder"] = name
    await query.edit_message_text(
        f"📂 已选择文件夹 *{name}*\n现在转发视频会自动存入这里。",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════
#  /get — 取出文件夹内所有视频
# ══════════════════════════════════════════

async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    args = context.args

    if not args:
        keyboard = await build_folder_keyboard(owner_id, "get")
        if not keyboard:
            await update.message.reply_text(
                "还没有文件夹，用 `/newfolder` 先创建一个。",
                parse_mode="Markdown",
            )
            return
        await update.message.reply_text("请选择要取出视频的文件夹：", reply_markup=keyboard)
        return

    name = " ".join(args)
    await send_folder_videos(update.message, owner_id, name, context)


async def cb_get_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    owner_id = query.from_user.id
    await query.edit_message_text(f"📤 正在发送文件夹 *{name}* 的视频…", parse_mode="Markdown")
    await send_folder_videos(query.message, owner_id, name, context)


async def send_folder_videos(message, owner_id: int, name: str, context):
    folder = await db_get_folder(owner_id, name)
    if not folder:
        await message.reply_text(f"❌ 找不到文件夹 *{name}*", parse_mode="Markdown")
        return

    videos = await db_get_videos(folder["id"])
    if not videos:
        await message.reply_text(
            f"📁 文件夹 *{name}* 是空的。",
            parse_mode="Markdown",
        )
        return

    await message.reply_text(f"📤 文件夹 *{name}* 共 {len(videos)} 个视频，正在发送…", parse_mode="Markdown")
    for v in videos:
        source = f"\n📡 来源：{v['forward_from']}" if v.get("forward_from") else ""
        cap = f"📁 {name}{source}\n{v['caption'] or ''}"
        try:
            await message.reply_video(video=v["file_id"], caption=cap.strip())
        except Exception as e:
            await message.reply_text(f"⚠️ 一个视频发送失败：{e}")


# ══════════════════════════════════════════
#  /rename — 重命名文件夹
# ══════════════════════════════════════════

async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("用法：`/rename 旧名称 新名称`", parse_mode="Markdown")
        return

    owner_id = update.effective_user.id
    old_name = context.args[0]
    new_name = " ".join(context.args[1:])
    result = await db_rename_folder(owner_id, old_name, new_name)

    if result == "ok":
        if context.user_data.get("current_folder") == old_name:
            context.user_data["current_folder"] = new_name
        await update.message.reply_text(
            f"✅ 已将文件夹 *{old_name}* 重命名为 *{new_name}*",
            parse_mode="Markdown",
        )
    elif result == "not_found":
        await update.message.reply_text(f"❌ 找不到文件夹 *{old_name}*", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ 文件夹 *{new_name}* 已存在，请换个名称。", parse_mode="Markdown")


# ══════════════════════════════════════════
#  /delFolder — 删除文件夹
# ══════════════════════════════════════════

async def cmd_del_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：`/delFolder 文件夹名称`", parse_mode="Markdown")
        return

    owner_id = update.effective_user.id
    name = " ".join(context.args)

    # 二次确认
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 确认删除", callback_data=f"delconfirm:{name}"),
        InlineKeyboardButton("❌ 取消", callback_data="delcancel"),
    ]])
    await update.message.reply_text(
        f"⚠️ 确定要删除文件夹 *{name}* 及其所有视频吗？",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def cb_del_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    owner_id = query.from_user.id
    ok = await db_delete_folder(owner_id, name)
    if ok:
        if context.user_data.get("current_folder") == name:
            context.user_data.pop("current_folder", None)
        await query.edit_message_text(f"🗑 文件夹 *{name}* 已删除。", parse_mode="Markdown")
    else:
        await query.edit_message_text(f"❌ 找不到文件夹 *{name}*", parse_mode="Markdown")


async def cb_del_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("已取消删除。")


# ══════════════════════════════════════════
#  接收视频
# ══════════════════════════════════════════

async def receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    video = message.video or message.document
    if not video:
        return

    owner_id = update.effective_user.id
    file_id   = video.file_id
    file_name = getattr(video, "file_name", None) or ""
    caption   = message.caption or ""
    forward_from = get_forward_source(message)

    current_folder = context.user_data.get("current_folder")

    if current_folder:
        # 已选文件夹 → 直接存入
        folder = await db_get_folder(owner_id, current_folder)
        if folder:
            await db_add_video(folder["id"], file_id, file_name, caption, forward_from)
            source_line = f"\n📡 来源：{forward_from}" if forward_from else ""
            await message.reply_text(
                f"✅ 已存入文件夹 *{current_folder}*{source_line}\n\n"
                f"继续转发视频，或用 `/open` 切换文件夹。",
                parse_mode="Markdown",
            )
            return
        else:
            # 文件夹已被删除，清空选择
            context.user_data.pop("current_folder", None)

    # 未选文件夹 → 弹出选择列表
    keyboard = await build_folder_keyboard(owner_id, "saveto")
    if not keyboard:
        await message.reply_text(
            "还没有文件夹！先用 `/newfolder` 创建一个，再转发视频。",
            parse_mode="Markdown",
        )
        return

    # 暂存视频信息等待用户选择
    context.user_data["pending_video"] = {
        "file_id": file_id,
        "file_name": file_name,
        "caption": caption,
        "forward_from": forward_from,
    }
    await message.reply_text("请选择要存入的文件夹：", reply_markup=keyboard)


async def cb_save_to_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    owner_id = query.from_user.id

    pending = context.user_data.pop("pending_video", None)
    if not pending:
        await query.edit_message_text("⚠️ 找不到待存视频，请重新转发。")
        return

    folder = await db_get_folder(owner_id, name)
    if not folder:
        await query.edit_message_text(f"❌ 找不到文件夹 *{name}*", parse_mode="Markdown")
        return

    await db_add_video(
        folder["id"],
        pending["file_id"],
        pending["file_name"],
        pending["caption"],
        pending.get("forward_from"),
    )
    source_line = f"\n📡 来源：{pending['forward_from']}" if pending.get("forward_from") else ""
    await query.edit_message_text(
        f"✅ 已存入文件夹 *{name}*{source_line}\n\n"
        f"💡 提示：用 `/open {name}` 选定文件夹，下次转发视频无需再选。",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════
#  主程序
# ══════════════════════════════════════════

async def post_init(app):
    await init_db()


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # 创建文件夹对话流程
    conv_newfolder = ConversationHandler(
        entry_points=[CommandHandler("newfolder", cmd_newfolder)],
        states={
            ASK_FOLDER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_folder_name)
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(conv_newfolder)

    # 命令
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("folders", cmd_folders))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(CommandHandler("delFolder", cmd_del_folder))

    # 视频接收
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, receive_video))

    # Inline 按钮回调
    app.add_handler(CallbackQueryHandler(cb_open_folder,    pattern=r"^open:"))
    app.add_handler(CallbackQueryHandler(cb_get_folder,     pattern=r"^get:"))
    app.add_handler(CallbackQueryHandler(cb_save_to_folder, pattern=r"^saveto:"))
    app.add_handler(CallbackQueryHandler(cb_del_confirm,    pattern=r"^delconfirm:"))
    app.add_handler(CallbackQueryHandler(cb_del_cancel,     pattern=r"^delcancel$"))

    logger.info("📁 文件夹视频机器人已启动")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
