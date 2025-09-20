"""
Telegram File Sharing Bot - Admin Only for Render Deployment
Features:
- Admin upload bot only
- Enhanced for Render hosting with proper process management
- Auto-restart functionality
- Improved large file support (up to 2GB)
- D1 database integration for member tracking

Requirements:
- Python 3.10+
- python-telegram-bot v20+ (asyncio interface)
- aiohttp for API requests

Install:
    pip install python-telegram-bot aiohttp --upgrade

Environment Variables (set in Render dashboard):
- ADMIN_BOT_TOKEN: Token for admin bot (file upload)
- ADMIN_ID: Your Telegram user ID
- STORAGE_CHAT_ID: Optional, for storing files
- BOT_USERNAME: Your bot username
- INTRO_IMAGE_URL: URL for intro image
- D1_API_KEY: Your D1 database API key
- D1_ACCOUNT_ID: Your D1 account ID
- D1_DATABASE_ID: Your D1 database ID

Run:
    python main.py
"""

import asyncio
import logging
import sqlite3
import time
import os
import aiohttp
import json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

# Path to the SQLite database file
DB_PATH = "files.db"

from telegram import Update, Document, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, FileSizeLimit
from telegram.helpers import escape_markdown
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler
from telegram.error import TelegramError, BadRequest

# -----------------------------
# CONFIG - from environment variables
# -----------------------------
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN", "8354069382:AAGymxQsztovYHbv0G9PeNEnQWrrdq9CszQ")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 6988262404))
STORAGE_CHAT_ID = os.environ.get("STORAGE_CHAT_ID", None)
STORAGE_CHAT_ID = int(STORAGE_CHAT_ID) if STORAGE_CHAT_ID else None
BOT_USERNAME = os.environ.get("BOT_USERNAME", "ANIME_VIVEX_BOT")
INTRO_IMAGE_URL = os.environ.get("INTRO_IMAGE_URL", "https://ik.imagekit.io/qoobmximx/tanjiro.jpg?updatedAt=1753792276550")

# D1 Database Configuration
D1_API_KEY = os.environ.get("D1_API_KEY", "T-s2oVvvh0GV9sQJzZ-mJPhk0XQppFMvq8WAaEQx")
D1_ACCOUNT_ID = os.environ.get("D1_ACCOUNT_ID", "26c0bbf6b8136d38f3e46d85f1121ece")
D1_DATABASE_ID = os.environ.get("D1_DATABASE_ID", "5dd5ef12-051e-45c0-8ad1-905c3e35c1a9")
D1_API_URL = f"https://dash.cloudflare.com/26c0bbf6b8136d38f3e46d85f1121ece/workers/d1/databases/5dd5ef12-051e-45c0-8ad1-905c3e35c1a9"

# Auto-restart settings
MAX_RESTART_ATTEMPTS = 10
RESTART_DELAY = 5  # seconds

# File size limits (Telegram bot API limit is 4GB = 4000MB)
MAX_FILE_SIZE = 50 * 1024 * 1024 * 1024  # 50 GB in bytes

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log")
    ]
)
logger = logging.getLogger(__name__)

# -----------------------------
# Database helpers
# -----------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_file_id TEXT NOT NULL,
            origin_filename TEXT,
            size_bytes INTEGER,
            uploader_id INTEGER,
            stored_chat_id INTEGER,
            stored_message_id INTEGER,
            created_at INTEGER,
            deleted INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def insert_file(tg_file_id: str, origin_filename: str, size_bytes: int, uploader_id: int, stored_chat_id: int, stored_message_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO files (tg_file_id, origin_filename, size_bytes, uploader_id, stored_chat_id, stored_message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tg_file_id, origin_filename, size_bytes, uploader_id, stored_chat_id, stored_message_id, int(time.time())),
    )
    fid = cur.lastrowid
    conn.commit()
    conn.close()
    return fid


def get_file_record(fid: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, tg_file_id, origin_filename, size_bytes, uploader_id, stored_chat_id, stored_message_id, created_at, deleted FROM files WHERE id = ? AND deleted = 0", (fid,))
    r = cur.fetchone()
    conn.close()
    return r


def get_all_files():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, tg_file_id, origin_filename, size_bytes, uploader_id, stored_chat_id, stored_message_id, created_at, deleted FROM files WHERE deleted = 0 ORDER BY created_at DESC")
    files = cur.fetchall()
    conn.close()
    return files


def mark_deleted(fid: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE files SET deleted = 1 WHERE id = ?", (fid,))
    conn.commit()
    conn.close()

# -----------------------------
# D1 Database helpers
# -----------------------------
async def query_d1_database(sql, params=None):
    """Execute a query on the D1 database"""
    headers = {
        "Authorization": f"Bearer {D1_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "sql": sql,
        "params": params or []
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(D1_API_URL, headers=headers, json=payload) as response:
                result = await response.json()
                if response.status == 200 and result.get("success"):
                    return result.get("result", [])
                else:
                    logger.error(f"D1 Database error: {result}")
                    return None
    except Exception as e:
        logger.error(f"Error querying D1 database: {e}")
        return None

async def get_members_count():
    """Get total number of members from D1 database"""
    result = await query_d1_database("SELECT COUNT(*) as count FROM members")
    if result and len(result) > 0:
        return result[0].get("count", 0)
    return 0

async def get_recent_members(limit=10):
    """Get recent members from D1 database"""
    result = await query_d1_database("SELECT * FROM members ORDER BY joined_at DESC LIMIT ?", [limit])
    return result or []

# -----------------------------
# Utility functions
# -----------------------------
def human_size(n: int) -> str:
    step = 1024
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(n)
    while size >= step and i < len(units) - 1:
        size /= step
        i += 1
    return f"{size:.2f} {units[i]}"


def format_timestamp(timestamp: int) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def build_caption(filename: str, size_bytes: int) -> str:
    size_hr = human_size(size_bytes)
    channel_invite = "https://t.me/+SJcUauglTx4yNGQ1"
    safe_filename = filename.replace("<", "").replace(">", "")
    safe_size = size_hr
    caption = (
        f"📂 ғɪʟᴇɴᴀᴍᴇ : {safe_filename}\n\n"
        f"⚙️ sɪᴢᴇ : {safe_size}\n\n"
        f"Jᴏɪɴ ᴜᴘᴅᴀᴛᴇ <a href=\"{channel_invite}\">ᴄʜᴀɴɴᴇʟ</a>\n\n"
        f"⚠️ This file is permanently available."
    )
    return caption

# -----------------------------
# Bot Handlers
# -----------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    
    # Create keyboard buttons for public commands
    keyboard = [
        [InlineKeyboardButton("👨‍💻 Support", callback_data="support"),
         InlineKeyboardButton("📢 Join Channel", url="https://t.me/+SJcUauglTx4yNGQ1")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if not args:
        intro_text = f"""
🎭 <b>Hello {user.first_name}!</b> 🎭

✦ Welcome to AMV VERSE BOT! ✦ 

↓ <b>Key Features:</b>
• 🔒 Secure file sharing
• ⚡ Fast and reliable transfers
• 📤 Easy upload for admins
• 🎨 Custom formatted captions
• 📁 Support for large files up to 2GB

📋 <b>How to use:</b>
• Admins can upload files directly to the bot
• Each file gets a unique sharing link
• Share the link with anyone
• Files are permanently available

 ➥ Join our channel for updates: @ANIME_VIVEX
        """
        
        try:
            await update.message.reply_photo(
                photo=INTRO_IMAGE_URL,
                caption=intro_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.warning(f"Could not send image, falling back to text: {e}")
            await update.message.reply_text(intro_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return
        
    payload = args[0]
    if not payload.startswith("file_"):
        await update.message.reply_text("Unknown start parameter.", reply_markup=reply_markup)
        return
    try:
        fid = int(payload.split("file_")[-1])
    except Exception:
        await update.message.reply_text("Invalid file id.", reply_markup=reply_markup)
        return
    rec = get_file_record(fid)
    if not rec:
        await update.message.reply_text("File not found or deleted.", reply_markup=reply_markup)
        return
    (id_, tg_file_id, origin_filename, size_bytes, uploader_id, stored_chat_id, stored_message_id, created_at, deleted) = rec
    if deleted:
        await update.message.reply_text("⚠️ This file has been deleted.", reply_markup=reply_markup)
        return
        
    caption = build_caption(origin_filename, size_bytes)
    try:
        await context.bot.send_document(
            chat_id=update.effective_chat.id, 
            document=tg_file_id, 
            caption=caption, 
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.exception("Failed to send document to user: %s", e)
        await update.message.reply_text("Failed to deliver the file. Please try again later.", reply_markup=reply_markup)


async def support_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    support_text = """
⚡ <b>Support</b>

If you need help or have questions, please contact our support team:

• ✉︎ Email: amvverse@proton.me
• 📢 Channel: https://t.me/+SJcUauglTx4yNGQ1
• 👥 Group: https://t.me/+YUDTOIBEztY5NmY1

We're here to help you!
    """
    
    # Create keyboard buttons for public commands
    keyboard = [
        [InlineKeyboardButton("🏠︎ Home", callback_data="start"),
        InlineKeyboardButton("◆︎ Join Channel", url="https://t.me/+SJcUauglTx4yNGQ1")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(support_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "start":
        # Create a fake update to reuse the start handler
        fake_update = Update(update.update_id, message=query.message)
        await start_handler(fake_update, context)
    elif query.data == "support":
        # Create a fake update to reuse the support handler
        fake_update = Update(update.update_id, message=query.message)
        await support_handler(fake_update, context)
    elif query.data == "get_file":
        await query.edit_message_text(
            "📂 To get a file, you need a direct link provided by an admin. "
            "Please contact the file owner for the download link.",
            parse_mode=ParseMode.HTML
        )


async def doc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("Only the admin may upload files.")
        return
    if not update.message.document:
        await update.message.reply_text("Please send a document/file as an attachment.")
        return
        
    doc: Document = update.message.document
    
    # Check file size
    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            f"❌ File too large. Maximum size is {human_size(MAX_FILE_SIZE)}. "
            f"Your file is {human_size(doc.file_size)}."
        )
        return
        
    store_chat = STORAGE_CHAT_ID or update.effective_chat.id

    try:
        # For large files, we need to use a different approach
        # First, forward the file to storage chat
        sent = await context.bot.forward_message(
            chat_id=store_chat,
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id
        )
        
        # Get the file_id from the forwarded message
        if sent.document:
            file_id = sent.document.file_id
        else:
            # If it's not a document, try to get it as a video (for MKV files)
            if sent.video:
                file_id = sent.video.file_id
            else:
                await update.message.reply_text("Unsupported file type. Please send a document or video file.")
                return

    except BadRequest as e:
        if "file is too big" in str(e).lower():
            await update.message.reply_text(
                f"❌ File too large for Telegram. Maximum size is {human_size(MAX_FILE_SIZE)}. "
                f"Your file is {human_size(doc.file_size)}."
            )
            return
        else:
            logger.exception("Failed to store document: %s", e)
            await update.message.reply_text("Failed to store the document. Make sure the bot can send messages to the storage chat.")
            return
    except Exception as e:
        logger.exception("Failed to store document: %s", e)
        await update.message.reply_text("Failed to store the document. Make sure the bot can send messages to the storage chat.")
        return

    fid = insert_file(file_id, doc.file_name or "unknown", doc.file_size or 0, user.id, sent.chat_id, sent.message_id)

    deeplink = f"https://t.me/{BOT_USERNAME}?start=file_{fid}"
    caption_preview = build_caption(doc.file_name or "unknown", doc.file_size or 0)
    
    reply_text = (
        "✔ <b>File Successfully Uploaded!</b>\n\n"
        "📊 <b>Details:</b>\n"
        "• 📂 File: {filename}\n"
        "• 📦 Size: {size}\n"
        "• 🔗 Permanent Link\n\n"
        "🔗 <b>Shareable Link:</b>\n"
        "{deeplink}\n\n"
        "📝 <b>Preview of caption users will see:</b>\n"
        "{caption_preview}"
    ).format(
        filename=escape_markdown(doc.file_name or "unknown", version=2),
        size=human_size(doc.file_size or 0),
        deeplink=deeplink,
        caption_preview=caption_preview
    )
    
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def files_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("Only the admin can view all files.")
        return
    
    files = get_all_files()
    
    if not files:
        await update.message.reply_text("📭 No files uploaded yet.")
        return
    
    response = "📁 <b>All Uploaded Files:</b>\n\n"
    
    for file in files:
        fid, tg_file_id, filename, size_bytes, uploader_id, stored_chat_id, stored_message_id, created_at, deleted = file
        deeplink = f"https://t.me/{BOT_USERNAME}?start=file_{fid}"
        size_hr = human_size(size_bytes)
        upload_time = format_timestamp(created_at)
        
        response += (
            f"📂 <b>File:</b> {escape_markdown(filename or 'unknown', version=2)}\n"
            f"📦 <b>Size:</b> {size_hr}\n"
            f"🆔 <b>ID:</b> {fid}\n"
            f"📅 <b>Uploaded:</b> {upload_time}\n"
            f"🔗 <b>Link:</b> {deeplink}\n"
            f"────────────────────\n\n"
        )
    
    if len(response) > 4096:
        parts = [response[i:i+4096] for i in range(0, len(response), 4096)]
        for part in parts:
            await update.message.reply_text(part, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(response, parse_mode=ParseMode.HTML)


async def members_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /members command - shows member statistics (admin only)"""
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("Only the admin can view member statistics.")
        return
    
    try:
        # Get member count from D1 database
        total_members = await get_members_count()
        
        # Get recent members
        recent_members = await get_recent_members(10)
        
        response = f"👥 <b>Member Statistics</b>\n\n"
        response += f"📊 <b>Total Members:</b> {total_members}\n\n"
        
        if recent_members:
            response += "🆕 <b>Recent Members:</b>\n"
            for i, member in enumerate(recent_members, 1):
                user_id = member.get("user_id", "N/A")
                username = member.get("username", "N/A")
                first_name = member.get("first_name", "N/A")
                joined_at = member.get("joined_at", "N/A")
                
                response += f"{i}. {first_name} (@{username}) - ID: {user_id}\n"
        else:
            response += "No recent members found.\n"
        
        await update.message.reply_text(response, parse_mode=ParseMode.HTML)
        
    except Exception as e:
        logger.error(f"Error retrieving member data: {e}")
        await update.message.reply_text("❌ Error retrieving member data. Please check the D1 database connection.")


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        # Public help message
        help_text = """
👨‍💻 <b>AMV VERSE BOT Help</b>

📋 <b>Available Commands:</b>
• /start - Show welcome message and bot info
• /support - Get support information

👥 <b>For Users:</b>
• Click on shared file links to download
• Files are permanently available

🔗 <b>Join our channel:</b> @ANIME_VIVEX
        """
        
        # Create keyboard buttons for public commands
        keyboard = [
            [InlineKeyboardButton("🏠︎ Home", callback_data="start"),
            InlineKeyboardButton("👨‍💻 Support", callback_data="support")],
            [InlineKeyboardButton("📢 Join Channel", url="https://t.me/+SJcUauglTx4yNGQ1")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return
    
    # Admin help message
    help_text = """
👨‍💻 <b>AMV VERSE BOT Help</b>

📋 <b>Available Commands:</b>
• /start - Show welcome message and bot info
• /help - Show this help message
• /status - Check bot status
• /files - Show all uploaded files (admin only)
• /members - Show member statistics (admin only)

👤 <b>For Admins:</b>
• Send any file to upload and get a shareable link
• Use /files to see all uploaded files
• Use /members to see member statistics
• Supports files up to 5GB in size

👥 <b>For Users:</b>
• Click on shared file links to download
• Files are permanently available

🔗 <b>Join our channel:</b> @ANIME_VIVEX
    """
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("Only the admin can check bot status.")
        return
        
    # Get member count from D1 database
    total_members = await get_members_count()
    
    status_text = f"""
✔ <b>Bot Status: Operational</b>

📈 <b>System Information:</b>
• 🔥 Bot: Running normally
• 💾 Database: Connected
• 📱 Mobile optimized: Yes
• 👥 Total Members: {total_members}

🛠️ <b>Technical Details:</b>
• File lifetime: Permanent
• Max file size: 2GB (Telegram limit)
• Storage: Secure Telegram servers
    """
    await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)


async def unknown_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Create keyboard buttons for public commands
    keyboard = [
        [InlineKeyboardButton("🏠︎︎ Home", callback_data="start"),
        InlineKeyboardButton("👨‍💻 Support", callback_data="support")],
        [InlineKeyboardButton("📢 Join Channel", url="https://t.me/+SJcUauglTx4yNGQ1")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "❓ Unknown command. Type /help to see available commands.",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )

# -----------------------------
# Bot Management
# -----------------------------
async def run_bot():
    """Run the bot with auto-restart capability"""
    restart_count = 0
    
    while restart_count < MAX_RESTART_ATTEMPTS:
        try:
            init_db()
            app = ApplicationBuilder().token(ADMIN_BOT_TOKEN).build()

            app.add_handler(CommandHandler("start", start_handler))
            app.add_handler(CommandHandler("help", help_handler))
            app.add_handler(CommandHandler("status", status_handler))
            app.add_handler(CommandHandler("support", support_handler))
            app.add_handler(CommandHandler("files", files_handler))
            app.add_handler(CommandHandler("members", members_handler))
            app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, doc_handler))
            app.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, doc_handler))  # Handle video files like MKV
            app.add_handler(CallbackQueryHandler(button_handler))
            app.add_handler(MessageHandler(filters.ALL, unknown_handler))

            logger.info("Starting bot...")
            await app.initialize()
            await app.start()
            await app.updater.start_polling(allowed_updates=["message", "edited_message", "callback_query"])
            
            # Keep the bot running until stopped
            while True:
                await asyncio.sleep(3600)
                
        except TelegramError as e:
            logger.error(f"Bot Telegram error: {e}")
            restart_count += 1
            logger.info(f"Restarting bot in {RESTART_DELAY} seconds (attempt {restart_count}/{MAX_RESTART_ATTEMPTS})...")
            await asyncio.sleep(RESTART_DELAY)
            
        except Exception as e:
            logger.error(f"Bot unexpected error: {e}")
            restart_count += 1
            logger.info(f"Restarting bot in {RESTART_DELAY} seconds (attempt {restart_count}/{MAX_RESTART_ATTEMPTS})...")
            await asyncio.sleep(RESTART_DELAY)
            
        finally:
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except:
                pass
    
    logger.error(f"Bot max restart attempts ({MAX_RESTART_ATTEMPTS}) reached. Exiting.")


async def main():
    """Run the bot"""
    await run_bot()


if __name__ == "__main__":
    # Create a simple HTTP server to keep the app alive on Render
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading
    
    class SimpleHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Bot is running')
    
    def run_http_server():
        server = HTTPServer(('0.0.0.0', 8080), SimpleHandler)
        print("HTTP server running on port 8080")
        server.serve_forever()
    
    # Start HTTP server in a separate thread
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()
    
    # Run the bot
    asyncio.run(main())