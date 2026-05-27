import logging
import os
import asyncio
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    ConversationHandler,
    filters,
)
import asyncpg

# 日志配置
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

# 读取环境变量
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# 会话状态定义
ASK_FOLDER_NAME = 1
ASK_RENAME_NAME = 2

# ==================== 数据库工具函数 ====================
async def get_db_conn():
    try:
        return await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        logging.error(f"数据库连接失败: {e}")
        return None

async def init_db():
    conn = await get_db_conn()
    if not conn:
        return
    try:
        # 文件夹表
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id SERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                UNIQUE(owner_id, name)
            )
        """)
        # 视频表
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id SERIAL PRIMARY KEY,
                folder_id INT REFERENCES folders(id) ON DELETE CASCADE,
                file_id TEXT NOT NULL,
                file_name TEXT,
                caption TEXT,
                forward_from TEXT
            )
        """)
        logging.info("✅ 数据表初始化完成")
    finally:
        await conn.close()

# ==================== 指令处理函数 ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """🤖 视频文件夹管理机器人
/start - 首页菜单
/help - 功能说明
/newfolder - 新建文件夹
/folders - 查看全部文件夹
/open 名称 - 打开指定文件夹
/list - 列出当前文件夹视频
/get ID - 获取对应视频
/delVideo ID - 删除视频
/rename 旧名称 - 重命名文件夹
/delFolder 名称 - 删除文件夹
/cancel - 取消操作

💡 打开文件夹后，直接发送视频即可自动保存
"""
    await update.message.reply_text(text)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

# 新建文件夹
async def cmd_newfolder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📁 请输入新文件夹名称：")
    return ASK_FOLDER_NAME

async def receive_folder_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    folder_name = update.message.text.strip()
    conn = await get_db_conn()
    if not conn:
        await update.message.reply_text("❌ 数据库异常")
        return ConversationHandler.END
    try:
        await conn.execute(
            "INSERT INTO folders (owner_id, name) VALUES ($1, $2)",
            user_id, folder_name
        )
        await update.message.reply_text(f"✅ 文件夹【{folder_name}】创建成功")
    except asyncpg.UniqueViolationError:
        await update.message.reply_text(f"❌ 文件夹【{folder_name}】已存在")
    except Exception:
        await update.message.reply_text("❌ 创建失败")
    finally:
        await conn.close()
    return ConversationHandler.END

# 取消操作
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ 操作已取消")
    return ConversationHandler.END

# 查看所有文件夹
async def cmd_folders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = await get_db_conn()
    if not conn:
        await update.message.reply_text("❌ 数据库异常")
        return
    try:
        rows = await conn.fetch("SELECT id, name FROM folders WHERE owner_id = $1", user_id)
        if not rows:
            await update.message.reply_text("📂 你还没有创建文件夹")
            return
        text = "📂 我的文件夹列表：\n"
        for idx, row in enumerate(rows, 1):
            text += f"{idx}. {row['name']}  (ID:{row['id']})\n"
        await update.message.reply_text(text)
    finally:
        await conn.close()

# 打开文件夹
async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ 格式：/open 文件夹名称")
        return
    folder_name = " ".join(context.args)
    user_id = update.effective_user.id
    conn = await get_db_conn()
    if not conn:
        await update.message.reply_text("❌ 数据库异常")
        return
    try:
        row = await conn.fetchrow(
            "SELECT id FROM folders WHERE owner_id = $1 AND name = $2",
            user_id, folder_name
        )
        if not row:
            await update.message.reply_text(f"❌ 未找到【{folder_name}】")
            return
        context.user_data["current_folder_id"] = row["id"]
        context.user_data["current_folder_name"] = folder_name
        await update.message.reply_text(f"✅ 已成功打开【{folder_name}】，可发送视频保存")
    finally:
        await conn.close()

# 接收并保存视频
async def receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "current_folder_id" not in context.user_data:
        await update.message.reply_text("⚠️ 请先使用 /open 打开一个文件夹")
        return
    folder_id = context.user_data["current_folder_id"]
    media = update.message.video or update.message.document
    if not media:
        return

    file_id = media.file_id
    file_name = media.file_name if hasattr(media, "file_name") else None
    caption = update.message.caption
    forward_from = str(update.message.forward_from.id) if update.message.forward_from else None

    conn = await get_db_conn()
    if not conn:
        await update.message.reply_text("❌ 数据库异常")
        return
    try:
        await conn.execute(
            """INSERT INTO videos (folder_id, file_id, file_name, caption, forward_from)
               VALUES ($1, $2, $3, $4, $5)""",
            folder_id, file_id, file_name, caption, forward_from
        )
        await update.message.reply_text("✅ 视频已保存")
    finally:
        await conn.close()

# 列出当前文件夹视频
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "current_folder_id" not in context.user_data:
        await update.message.reply_text("⚠️ 请先打开文件夹")
        return
    folder_id = context.user_data["current_folder_id"]
    conn = await get_db_conn()
    if not conn:
        await update.message.reply_text("❌ 数据库异常")
        return
    try:
        rows = await conn.fetch("SELECT id, file_name FROM videos WHERE folder_id = $1", folder_id)
        if not rows:
            await update.message.reply_text("📹 文件夹内暂无视频")
            return
        text = "📹 视频列表：\n"
        for item in rows:
            name = item["file_name"] or "未命名"
            text += f"ID：{item['id']} | {name}\n"
        await update.message.reply_text(text)
    finally:
        await conn.close()

# 获取视频
async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ 格式：/get 视频ID")
        return
    try:
        video_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID 必须为数字")
        return

    folder_id = context.user_data.get("current_folder_id")
    if not folder_id:
        await update.message.reply_text("⚠️ 请先打开文件夹")
        return

    conn = await get_db_conn()
    if not conn:
        await update.message.reply_text("❌ 数据库异常")
        return
    try:
        row = await conn.fetchrow(
            "SELECT file_id, caption FROM videos WHERE id = $1 AND folder_id = $2",
            video_id, folder_id
        )
        if not row:
            await update.message.reply_text("❌ 未找到该视频")
            return
        await context.bot.send_video(
            chat_id=update.effective_chat.id,
            video=row["file_id"],
            caption=row["caption"]
        )
    finally:
        await conn.close()

# 删除视频
async def cmd_delVideo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ 格式：/delVideo 视频ID")
        return
    try:
        video_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID 必须为数字")
        return

    folder_id = context.user_data.get("current_folder_id")
    if not folder_id:
        await update.message.reply_text("⚠️ 请先打开文件夹")
        return

    conn = await get_db_conn()
    if not conn:
        await update.message.reply_text("❌ 数据库异常")
        return
    try:
        res = await conn.execute(
            "DELETE FROM videos WHERE id = $1 AND folder_id = $2",
            video_id, folder_id
        )
        if "DELETE 1" in res:
            await update.message.reply_text("✅ 视频已删除")
        else:
            await update.message.reply_text("❌ 删除失败，检查ID")
    finally:
        await conn.close()

# 重命名文件夹
async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ 格式：/rename 原文件夹名称")
        return
    context.user_data["old_folder_name"] = " ".join(context.args)
    await update.message.reply_text("📝 请输入新的文件夹名称：")
    return ASK_RENAME_NAME

async def receive_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    old_name = context.user_data.get("old_folder_name")
    new_name = update.message.text.strip()

    if not old_name:
        await update.message.reply_text("❌ 操作异常")
        return ConversationHandler.END

    conn = await get_db_conn()
    if not conn:
        await update.message.reply_text("❌ 数据库异常")
        return ConversationHandler.END
    try:
        await conn.execute(
            "UPDATE folders SET name = $1 WHERE owner_id = $2 AND name = $3",
            new_name, user_id, old_name
        )
        await update.message.reply_text(f"✅ 已将【{old_name}】重命名为【{new_name}】")
        # 同步更新当前文件夹标记
        if context.user_data.get("current_folder_name") == old_name:
            context.user_data["current_folder_name"] = new_name
    except asyncpg.UniqueViolationError:
        await update.message.reply_text(f"❌ 名称【{new_name}】已存在")
    except Exception:
        await update.message.reply_text("❌ 重命名失败")
    finally:
        await conn.close()
        context.user_data.pop("old_folder_name", None)
    return ConversationHandler.END

# 删除文件夹（连带视频）
async def cmd_delFolder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ 格式：/delFolder 文件夹名称")
        return
    folder_name = " ".join(context.args)
    user_id = update.effective_user.id

    conn = await get_db_conn()
    if not conn:
        await update.message.reply_text("❌ 数据库异常")
        return
    try:
        res = await conn.execute(
            "DELETE FROM folders WHERE owner_id = $1 AND name = $2",
            user_id, folder_name
        )
        if "DELETE 1" in res:
            # 清空当前文件夹记录
            if context.user_data.get("current_folder_name") == folder_name:
                context.user_data.clear()
            await update.message.reply_text(f"✅ 文件夹【{folder_name}】及内部视频已删除")
        else:
            await update.message.reply_text(f"❌ 未找到【{folder_name}】")
    finally:
        await conn.close()

# ==================== 程序入口（修复事件循环） ====================
def main():
    # 修复 Python3.14 事件循环报错
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    # 初始化数据库表
    loop.run_until_complete(init_db())

    # 初始化机器人
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # 会话处理器：新建文件夹
    new_folder_conv = ConversationHandler(
        entry_points=[CommandHandler("newfolder", cmd_newfolder)],
        states={
            ASK_FOLDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_folder_name)]
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)]
    )

    # 会话处理器：重命名文件夹
    rename_conv = ConversationHandler(
        entry_points=[CommandHandler("rename", cmd_rename)],
        states={
            ASK_RENAME_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_name)]
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)]
    )

    # 注册所有功能
    app.add_handler(new_folder_conv)
    app.add_handler(rename_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("folders", cmd_folders))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("delVideo", cmd_delVideo))
    app.add_handler(CommandHandler("delFolder", cmd_delFolder))
    # 监听视频消息
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, receive_video))

    # 启动轮询
    loop.run_until_complete(app.run_polling())

if __name__ == "__main__":
    main()
