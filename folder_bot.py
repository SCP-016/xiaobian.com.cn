import logging
import os
import asyncio
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
ASK_FOLDER_NAME = 1

async def start(update: Update, context: ContextTypes):
    await update.message.reply_text("✅ 机器人启动成功！能用啦！")

async def newfolder(update: Update, context: ContextTypes):
    await update.message.reply_text("请输入文件夹名称")
    return ASK_FOLDER_NAME

async def save_folder(update: Update, context: ContextTypes):
    await update.message.reply_text("✅ 创建成功！")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes):
    await update.message.reply_text("✅ 已取消")
    return ConversationHandler.END

def main():
    # 修复 Render Python 3.14 事件循环报错
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("newfolder", newfolder)],
        states={
            ASK_FOLDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_folder)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))

    loop.run_until_complete(app.run_polling())

if __name__ == "__main__":
    main()
