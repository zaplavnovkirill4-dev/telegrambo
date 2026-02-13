import logging
import random
import string
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = "8453970786:AAFM5UZQoMaqY1PAIj1sAtt0Xcrm1inrKnI"
DB_FILE = "telegram_bot.db"
CAPTCHA_LENGTH = 6
COOLDOWN_MINUTES = 5

# Хранилище активных капч в памяти
user_captchas = {}

# ==================== DATABASE ====================
class Database:
    @staticmethod
    def init():
        """Инициализация базы данных"""
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_access TIMESTAMP,
                    UNIQUE(user_id)
                )
            ''')
            # Создаем индекс для быстрого поиска по last_access
            conn.execute('CREATE INDEX IF NOT EXISTS idx_last_access ON users(last_access)')
            conn.commit()
    
    @staticmethod
    def is_registered(user_id):
        """Проверка регистрации пользователя"""
        with sqlite3.connect(DB_FILE) as conn:
            result = conn.execute('SELECT 1 FROM users WHERE user_id = ? LIMIT 1', (user_id,)).fetchone()
            return result is not None
    
    @staticmethod
    def can_access(user_id):
        """Проверка возможности доступа (прошло ли 5 минут)"""
        with sqlite3.connect(DB_FILE) as conn:
            result = conn.execute('SELECT last_access FROM users WHERE user_id = ?', (user_id,)).fetchone()
            
            if not result or not result[0]:
                return True
            
            last_access = datetime.fromisoformat(result[0])
            return datetime.now() - last_access >= timedelta(minutes=COOLDOWN_MINUTES)
    
    @staticmethod
    def register(user_id, username, first_name, last_name):
        """Регистрация или обновление пользователя"""
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute('''
                INSERT INTO users (user_id, username, first_name, last_name, last_access)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    last_access = excluded.last_access
            ''', (user_id, username, first_name, last_name, datetime.now().isoformat()))
            conn.commit()

# ==================== CAPTCHA ====================
class CaptchaGenerator:
    # Исключаем похожие символы
    CHARS = string.ascii_uppercase + string.digits
    CHARS = CHARS.replace('O', '').replace('0', '').replace('I', '').replace('1', '').replace('Q', '')
    
    @staticmethod
    def generate_text(length=CAPTCHA_LENGTH):
        """Генерация случайного текста капчи"""
        return ''.join(random.choice(CaptchaGenerator.CHARS) for _ in range(length))
    
    @staticmethod
    def create_image(text):
        """Создание изображения капчи"""
        width, height = 300, 100
        image = Image.new('RGB', (width, height), color='white')
        draw = ImageDraw.Draw(image)
        
        # Загрузка шрифта
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
        except:
            try:
                font = ImageFont.truetype("arial.ttf", 40)
            except:
                font = ImageFont.load_default()
        
        # Фоновые линии
        for _ in range(5):
            coords = [(random.randint(0, width), random.randint(0, height)) for _ in range(2)]
            draw.line(coords, fill='lightgray', width=2)
        
        # Текст с искажением
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
        except:
            text_width, text_height = 150, 40
        
        x = (width - text_width) // 2
        y = (height - text_height) // 2
        
        for i, char in enumerate(text):
            char_x = x + i * (text_width // len(text))
            char_y = y + random.randint(-5, 5)
            draw.text((char_x, char_y), char, fill='black', font=font)
        
        # Шум (точки)
        for _ in range(100):
            draw.point((random.randint(0, width), random.randint(0, height)), fill='gray')
        
        # Сохранение в BytesIO
        bio = BytesIO()
        bio.name = 'captcha.png'
        image.save(bio, 'PNG')
        bio.seek(0)
        return bio

# ==================== HANDLERS ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    user_id = user.id
    
    # Проверка доступа
    if Database.is_registered(user_id) and not Database.can_access(user_id):
        await update.message.reply_text("🚫 Вы уже получали переход, приходите через 5 минут.")
        return
    
    # Генерация и отправка капчи
    captcha_text = CaptchaGenerator.generate_text()
    captcha_image = CaptchaGenerator.create_image(captcha_text)
    
    keyboard = [[InlineKeyboardButton("🔄 Обновить капчу", callback_data="refresh_captcha")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = await update.message.reply_photo(
        photo=captcha_image,
        caption="🔐 Введите текст с изображения:",
        reply_markup=reply_markup
    )
    
    # Сохранение данных капчи
    user_captchas[user_id] = {
        'captcha': captcha_text,
        'message_ids': [message.message_id],
        'chat_id': message.chat_id
    }

async def refresh_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик обновления капчи"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    # Генерация новой капчи
    captcha_text = CaptchaGenerator.generate_text()
    captcha_image = CaptchaGenerator.create_image(captcha_text)
    
    keyboard = [[InlineKeyboardButton("🔄 Обновить капчу", callback_data="refresh_captcha")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Обновление изображения в том же сообщении
    try:
        await query.message.edit_media(
            media=InputMediaPhoto(
                media=captcha_image,
                caption="🔐 Введите текст с изображения:"
            ),
            reply_markup=reply_markup
        )
        
        # Обновление данных в хранилище
        if user_id in user_captchas:
            user_captchas[user_id]['captcha'] = captcha_text
        else:
            user_captchas[user_id] = {
                'captcha': captcha_text,
                'message_ids': [query.message.message_id],
                'chat_id': query.message.chat_id
            }
    except Exception as e:
        logger.error(f"Ошибка обновления капчи для {user_id}: {e}")

async def check_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка введенной капчи"""
    user = update.effective_user
    user_id = user.id
    user_text = update.message.text.strip().upper()
    
    # Проверка наличия активной капчи
    if user_id not in user_captchas:
        return
    
    captcha_data = user_captchas[user_id]
    correct_captcha = captcha_data['captcha']
    
    if user_text == correct_captcha:
        # ✅ Капча верна - удаляем все сообщения
        await _handle_success(update, context, user, captcha_data)
    else:
        # ❌ Капча неверна - показываем ошибку
        await _handle_error(update, context, user_id, captcha_data)

async def _handle_success(update, context, user, captcha_data):
    """Обработка успешного ввода капчи"""
    user_id = user.id
    
    # Удаление всех сообщений бота
    for msg_id in captcha_data['message_ids']:
        try:
            await context.bot.delete_message(chat_id=captcha_data['chat_id'], message_id=msg_id)
        except Exception as e:
            logger.warning(f"Не удалось удалить сообщение {msg_id}: {e}")
    
    # Удаление сообщения пользователя
    try:
        await update.message.delete()
    except:
        pass
    
    # Регистрация пользователя
    Database.register(user_id, user.username, user.first_name, user.last_name)
    
    # Очистка данных капчи
    del user_captchas[user_id]
    
    # Отправка финального сообщения
    keyboard = [[InlineKeyboardButton("TESS | ПЕРЕХОДНИК 🚀", url="https://t.me/+xOW2CVdP6sNiM2Vi")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🤗 TESS - это бесконечность.\n\n❗️ Новый ссылки можно получить через 5 минут.",
        reply_markup=reply_markup
    )

async def _handle_error(update, context, user_id, captcha_data):
    """Обработка неверного ввода капчи"""
    # Удаление неверного ответа пользователя
    try:
        await update.message.delete()
    except:
        pass
    
    # Отправка сообщения об ошибке
    error_message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="❌ Капча введена неверно\n😦 Отправьте текст капчи:"
    )
    
    # Добавление ID сообщения об ошибке для последующего удаления
    if user_id in user_captchas:
        user_captchas[user_id]['message_ids'].append(error_message.message_id)

# ==================== MAIN ====================
def main():
    """Главная функция запуска бота"""
    # Инициализация БД
    Database.init()
    
    # Создание приложения
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(refresh_captcha, pattern="^refresh_captcha$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_captcha))
    
    # Запуск бота
    logger.info("🚀 Бот запущен и готов к работе!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
