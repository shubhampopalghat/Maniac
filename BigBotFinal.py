import warnings
warnings.filterwarnings('ignore', category=UserWarning, module='pkg_resources')

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, 
    CallbackQueryHandler, filters
)
from telegram.constants import ParseMode
from telethon.sync import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from telethon import events
from telethon.tl.types import MessageService
import os
import json
import tempfile
import zipfile
import shutil
import re
import asyncio
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
API_ID = 21021767
API_HASH = "f0d2874afa840c35b1c96400212a78d3"
SESSIONS_DIR = 'sessions'

# Active login sessions for OTP detection
active_sessions = {}
message_handlers = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    await update.message.reply_text(
        "👋 **Welcome to the Account Manager Bot!**\n\n"
        "Send me a ZIP file containing your authorized account session files.\n\n"
        "📋 **ZIP Format:**\n"
        "```\n"
        "accounts.zip\n"
        "├── 14944888484.json\n"
        "├── 14944888484.session\n"
        "├── 44858938484.json\n"
        "└── 44858938484.session\n"
        "```\n\n"
        "💡 The bot will monitor these authorized accounts for OTP codes when you try to login.",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_otp_message(event):
    """Handle incoming OTP messages from Telegram"""
    try:
        if not event.message or not event.message.message:
            return

        # Skip service messages (like login notifications)
        if isinstance(event.message, MessageService):
            return

        msg_text = event.message.message
        logger.info(f"Received message: {msg_text}")
        
        # Look for the specific OTP message format
        if "Login code:" in msg_text and "Do not give this code to anyone" in msg_text:
            # Extract the 5-digit code using regex
            code_match = re.search(r'Login code: (\d{5})', msg_text)
            if code_match:
                otp_code = code_match.group(1)
                logger.info(f"Detected OTP code: {otp_code}")
                
                user_id = active_sessions.get('current_user')
                bot = active_sessions.get('bot')
                current_phone = active_sessions.get('phone')
                twofa = active_sessions.get('twofa', '')
                
                if user_id and bot:
                    # Build keyboard with options
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("Next Account", callback_data="next_account")],
                        [InlineKeyboardButton("Capture OTP", callback_data="capture_otp")],
                        [InlineKeyboardButton("Stop", callback_data="stop_process")]
                    ])

                    # Send the OTP information to the user
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"🔐 **OTP Received!**\n\n"
                             f"📱 Number: `{current_phone}`\n"
                             f"🔢 Login Code: `{otp_code}`\n"
                             f"🔑 2FA: `{twofa}`\n\n"
                             f"💬 Message:\n{msg_text}",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
    except Exception as e:
        logger.error(f"Error in OTP detection: {e}")

async def capture_recent_otp():
    """Capture the most recent OTP message from Telegram"""
    try:
        client = active_sessions.get('client')
        if not client:
            return None, None
            
        # Get the most recent messages (last 10)
        messages = await client.get_messages('Telegram', limit=10)
        
        # Look for OTP messages in the recent messages
        for message in messages:
            if not message.message:
                continue
                
            msg_text = message.message
            # Look for the specific OTP message format
            if "Login code:" in msg_text and "Do not give this code to anyone" in msg_text:
                # Extract the 5-digit code using regex
                code_match = re.search(r'Login code: (\d{5})', msg_text)
                if code_match:
                    otp_code = code_match.group(1)
                    logger.info(f"Captured OTP code from recent messages: {otp_code}")
                    return msg_text, otp_code
                    
        return None, None
    except Exception as e:
        logger.error(f"Error capturing recent OTP: {e}")
        return None, None

async def process_next_account(user_id, bot):
    """Process the next authorized account in the queue"""
    # Clean up previous client if exists
    if active_sessions.get('client'):
        try:
            client = active_sessions.get('client')
            if client:
                # Remove message handler
                phone = active_sessions.get('phone')
                if phone and phone in message_handlers:
                    client.remove_event_handler(message_handlers[phone])
                    del message_handlers[phone]
                
                # Disconnect client
                await client.disconnect()
        except Exception as e:
            logger.error(f"Error cleaning up client: {e}")
    
    accounts = active_sessions.get('pending_accounts', [])
    if not accounts:
        await bot.send_message(chat_id=user_id, text="✅ All authorized accounts processed!")
        active_sessions.clear()
        return

    # Get next account
    next_account = accounts.pop(0)
    active_sessions['pending_accounts'] = accounts

    phone = next_account.get('phone')
    twofa = next_account.get('twofa')
    session_path = next_account.get('session_path')

    # Tell user to use this account for login
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Capture OTP", callback_data="capture_otp")],
        [InlineKeyboardButton("Next Account", callback_data="next_account")],
        [InlineKeyboardButton("Stop", callback_data="stop_process")]
    ])
    
    message = await bot.send_message(
        chat_id=user_id,
        text=f"📱 **Use this authorized account to log in:** `{phone}`\n\n"
             f"🔑 **2FA (if asked):** `{twofa}`\n\n"
             f"⏳ I will monitor this account for OTP messages when you try to login.",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )
    
    active_sessions['current_message_id'] = message.message_id

    try:
        # Initialize client with the existing session
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()

        # Check if authorized (should be since we filtered)
        if not await client.is_user_authorized():
            await bot.send_message(chat_id=user_id, text=f"❌ Account {phone} is not authorized! Skipping...")
            await client.disconnect()
            await process_next_account(user_id, bot)
            return

        # Store client for OTP detection
        active_sessions.update({
            'current_user': user_id,
            'bot': bot,
            'client': client,
            'phone': phone,
            'twofa': twofa,
            'session_path': session_path
        })
        
        # Add message handler for OTP detection
        @client.on(events.NewMessage(incoming=True))
        async def new_message_handler(event):
            await handle_otp_message(event)
        
        message_handlers[phone] = new_message_handler
        
        # Start listening for messages
        client.start()
        
        await bot.send_message(
            chat_id=user_id,
            text=f"🔍 Now monitoring {phone} for OTP messages. Please try to login with this number in your Telegram app."
        )
        
    except Exception as e:
        error_msg = f"❌ Error with {phone}: {str(e)}"
        await bot.send_message(chat_id=user_id, text=error_msg)
        await asyncio.sleep(2)
        await process_next_account(user_id, bot)

async def handle_zip_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ZIP file upload containing authorized session files"""
    user_id = update.effective_user.id
    
    # Clear any existing sessions
    if active_sessions.get('client'):
        try:
            client = active_sessions.get('client')
            if client:
                # Remove message handler
                phone = active_sessions.get('phone')
                if phone and phone in message_handlers:
                    client.remove_event_handler(message_handlers[phone])
                    del message_handlers[phone]
                
                # Disconnect client
                await client.disconnect()
        except Exception as e:
            logger.error(f"Error cleaning up client: {e}")
    active_sessions.clear()
    
    if not update.message.document:
        await update.message.reply_text(
            "❌ Please send a ZIP file containing authorized session files",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if not update.message.document.file_name.endswith('.zip'):
        await update.message.reply_text(
            "❌ File must be a ZIP archive",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Create temp directory for processing
    temp_dir = tempfile.mkdtemp()
    try:
        # Download and extract ZIP
        file = await context.bot.get_file(update.message.document.file_id)
        zip_path = os.path.join(temp_dir, "accounts.zip")
        await file.download_to_drive(zip_path)
        
        # Process ZIP file
        accounts = []
        
        await update.message.reply_text(
            "🔍 **Checking authorized accounts in ZIP file...**\n"
            "_This might take a moment..._",
            parse_mode=ParseMode.MARKDOWN
        )
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
            
            # Find JSON files and corresponding sessions
            json_files = [f for f in os.listdir(temp_dir) if f.endswith('.json')]
            for json_file in json_files:
                phone = json_file.replace('.json', '')
                session_file = f"{phone}.session"
                
                if session_file not in os.listdir(temp_dir):
                    continue
                
                # Read account data from JSON
                with open(os.path.join(temp_dir, json_file), 'r') as f:
                    account_data = json.load(f)
                
                # Create user directory if needed
                user_dir = os.path.join(SESSIONS_DIR, str(user_id))
                os.makedirs(user_dir, exist_ok=True)
                
                # Copy files to user directory
                session_path = os.path.join(user_dir, phone)
                shutil.copy2(
                    os.path.join(temp_dir, session_file),
                    f"{session_path}.session"
                )
                shutil.copy2(
                    os.path.join(temp_dir, json_file),
                    f"{session_path}.json"
                )
                
                # Validate session - only add authorized accounts
                try:
                    test_client = TelegramClient(session_path, API_ID, API_HASH)
                    await test_client.connect()
                    
                    if await test_client.is_user_authorized():
                        accounts.append({
                            'phone': account_data.get('phone', phone),
                            'twofa': account_data.get('twoFA', account_data.get('twofa', '')),
                            'session_path': session_path,
                            'authorized': True
                        })
                        logger.info(f"Added authorized account: {phone}")
                    else:
                        logger.info(f"Skipping unauthorized account: {phone}")
                    
                    await test_client.disconnect()
                except Exception as e:
                    logger.error(f"Error validating session {phone}: {e}")
        
        if accounts:
            # Only process authorized accounts
            await update.message.reply_text(
                f"✅ Found {len(accounts)} authorized accounts. Starting OTP monitoring...",
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Store accounts in session and start processing
            active_sessions['pending_accounts'] = accounts
            await process_next_account(user_id, context.bot)
        else:
            await update.message.reply_text(
                "❌ No authorized accounts found in the ZIP file",
                parse_mode=ParseMode.MARKDOWN
            )
            
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Error processing ZIP file:** {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )
    finally:
        # Cleanup
        try:
            shutil.rmtree(temp_dir)
        except:
            pass

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    data = query.data

    user_id = query.from_user.id
    bot = context.bot

    # Stop process
    if data == "stop_process":
        try:
            client = active_sessions.get('client')
            if client:
                # Remove message handler
                phone = active_sessions.get('phone')
                if phone and phone in message_handlers:
                    client.remove_event_handler(message_handlers[phone])
                    del message_handlers[phone]
                
                # Disconnect client
                await client.disconnect()
        except Exception as e:
            logger.error(f"Error cleaning up client: {e}")
        active_sessions.clear()
        await query.edit_message_text("🛑 Process stopped.")
        return

    # Next account
    if data == "next_account":
        await query.edit_message_text("⏭️ Moving to next account...")
        await process_next_account(user_id, bot)
        return

    # Capture OTP
    if data == "capture_otp":
        client = active_sessions.get('client')
        phone = active_sessions.get('phone')
        twofa = active_sessions.get('twofa')
        
        if client and phone:
            try:
                await query.answer("🔍 Checking for OTP messages...")
                
                # Capture the most recent OTP
                msg_text, otp_code = await capture_recent_otp()
                
                if msg_text and otp_code:
                    # Build keyboard with options
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("Next Account", callback_data="next_account")],
                        [InlineKeyboardButton("Capture OTP", callback_data="capture_otp")],
                        [InlineKeyboardButton("Stop", callback_data="stop_process")]
                    ])

                    # Send the OTP information to the user
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"🔐 **OTP Captured!**\n\n"
                             f"📱 Number: `{phone}`\n"
                             f"🔢 Login Code: `{otp_code}`\n"
                             f"🔑 2FA: `{twofa}`\n\n"
                             f"💬 Message:\n{msg_text}",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await query.answer("❌ No OTP found in recent messages")
                    await bot.send_message(
                        chat_id=user_id,
                        text="❌ No OTP code found in recent messages. Please try to login with this number in your Telegram app first."
                    )
            except Exception as e:
                await query.answer(f"❌ Error: {str(e)}")
        else:
            await query.answer("❌ No active session to capture OTP")
        return

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular messages - for manual OTP entry"""
    # If user sends a message, check if it might be an OTP
    message_text = update.message.text
    if message_text and re.match(r'^\d{5}$', message_text.strip()):
        # Might be an OTP entered manually
        user_id = update.effective_user.id
        if user_id == active_sessions.get('current_user'):
            phone = active_sessions.get('phone')
            twofa = active_sessions.get('twofa')
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Next Account", callback_data="next_account")],
                [InlineKeyboardButton("Stop", callback_data="stop_process")]
            ])
            
            await update.message.reply_text(
                f"🔐 **OTP Entered Manually**\n\n"
                f"📱 Number: `{phone}`\n"
                f"🔢 Login Code: `{message_text.strip()}`\n"
                f"🔑 2FA: `{twofa}`",
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )

def main():
    """Start the bot."""
    # Create directories if needed
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    
    # Load config
    if os.path.exists('botConfigManiac.json'):
        with open('botConfigManiac.json', 'r') as f:
            config = json.load(f)
            TOKEN = config.get('BOT_TOKEN')
    else:
        print("Please create botConfigManiac.json with your bot token")
        return
    
    # Create application
    application = Application.builder().token(TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_zip_upload))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Start bot
    print("Bot started...")
    application.run_polling()

if __name__ == '__main__':
    main()