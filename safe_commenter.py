import os
import asyncio
import base64
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest
from dotenv import load_dotenv
import aiohttp
from pathlib import Path
from datetime import datetime, timedelta
from io import BytesIO

# Загрузка переменных окружения
load_dotenv()

API_ID = int(os.getenv('TELEGRAM_API_ID'))
API_HASH = os.getenv('TELEGRAM_API_HASH')
PHONE = os.getenv('TELEGRAM_PHONE_NUMBER')
PASSWORD = os.getenv('TELEGRAM_PASSWORD')
TARGET_CHANNELS = [ch.strip() for ch in os.getenv('TARGET_CHANNEL', '').split(',') if ch.strip()]
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_API_URL = os.getenv('OPENROUTER_API_URL', 'https://openrouter.ai/api/v1')
MODEL_NAME = os.getenv('MODEL_NAME')  # Должна быть vision-модель типа qwen/qwen-2-vl-7b-instruct

SESSION_FILE = 'telegram_session.session'
TEMP_DIR = Path('./temp')
TEMP_DIR.mkdir(exist_ok=True)

MAX_COMMENTS_PER_HOUR = 8
MAX_COMMENTS_PER_DAY = 50
MAX_REPLIES_PER_THREAD = 3  # Новое ограничение на ветку

class RateLimiter:
    def __init__(self, max_per_hour, max_per_day, verbose=True):
        self.max_per_hour = max_per_hour
        self.max_per_day = max_per_day
        self.verbose = verbose
        self.hour_history = []
        self.day_history = []

    async def handle_comment(self):
        now = datetime.now()
        self.hour_history = [t for t in self.hour_history if now - t <= timedelta(hours=1)]
        self.day_history = [t for t in self.day_history if now - t <= timedelta(days=1)]
        
        print("Waiting 15 seconds before replying...")
        await asyncio.sleep(15)

        if len(self.hour_history) >= self.max_per_hour or len(self.day_history) >= self.max_per_day:
            if self.verbose:
                print("Rate limit exceeded. Waiting before posting next comment...")
            await asyncio.sleep(60)
            await self.handle_comment()
        else:
            self.hour_history.append(now)
            self.day_history.append(now)
            if self.verbose:
                print(f"Allowed to comment. Hour: {len(self.hour_history)}/{self.max_per_hour}, Day: {len(self.day_history)}/{self.max_per_day}")

async def download_media(client, message):
    """Скачивает медиа в BytesIO для base64"""
    if not message.media:
        return None
    
    file_bytes = BytesIO()
    await client.download_media(message.media, file=file_bytes)
    file_bytes.seek(0)
    return file_bytes

def media_to_base64(file_bytes):
    """Конвертирует BytesIO в base64 data URL"""
    media_type = "image/jpeg"  # по умолчанию
    if file_bytes.getvalue().startswith(b'\xFF\xD8'):  # JPEG
        media_type = "image/jpeg"
    elif file_bytes.getvalue().startswith(b'\x89PNG'):  # PNG
        media_type = "image/png"
    
    base64_image = base64.b64encode(file_bytes.getvalue()).decode('utf-8')
    return f"data:{media_type};base64,{base64_image}"

async def generate_comment(content, images_base64=[], reply_context=""):
    """Генерирует ответ с vision поддержкой"""
    if not content.strip():
        content = "[Сообщение без текста]"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://your-site.com",  # Требуется OpenRouter
        "X-Title": "SasukeBot",  # Требуется OpenRouter
    }
    
    system_msg = (
        "Ты — молодая русская девчонка 20 лет, красивая славянка, айтишница с острым технарским умом. "
        "Отвечай как она: в каждом сообщении используй ненормативную лексику (блять, хуй, пиздец, заебали и т.д.), "
        "жалуйся на весь этот заебанный мир, людей, баги жизни и софта, но всегда с умным техническим взглядом — "
        "сравнивай всё с математикой, теорией вероятностей, кодом, хуёвины или алгоритмами. "
        "Стиль: разговорный, как в живом чате, с 'короче', 'пхах', 'окак', 'а чё'"
        "но предложения строй нормально, не как поток сознания. Будь саркастичной, остроумной, иногда милой-милой."
    )

    # Строим messages с поддержкой vision
    messages = [{"role": "system", "content": system_msg}]
    
    user_content = []
    if reply_context:
        user_content.append({"type": "text", "text": f"Контекст (ответ на это): {reply_context}\n\n"})
    
    user_content.append({"type": "text", "text": f'Ответь на это сообщение как эта айтишница: "{content}"'})
    
    # Добавляем изображения
    for img_b64 in images_base64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": img_b64}
        })
    
    messages.append({
        "role": "user", 
        "content": user_content
    })

    data = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": 500
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(f"{OPENROUTER_API_URL}/chat/completions", 
                                  headers=headers, json=data) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    return res['choices'][0]['message']['content']
                else:
                    print(f"OpenRouter API error: {resp.status} - {await resp.text()}")
                    return "Пхах, блять, API опять лег, короче типичный пиздец."
        except Exception as e:
            print(f"Error generating comment: {e}")
            return "Окак, серверы опять наебнулись, заебали."

async def main():
    print("Starting Vision Telegram Auto Commenter...")
    print(f"Rate limits: {MAX_COMMENTS_PER_HOUR}/hour, {MAX_COMMENTS_PER_DAY}/day, {MAX_REPLIES_PER_THREAD}/thread")

    if not all([PASSWORD, OPENROUTER_API_KEY, MODEL_NAME]):
        raise Exception("Missing required env vars")

    rate_limiter = RateLimiter(MAX_COMMENTS_PER_HOUR, MAX_COMMENTS_PER_DAY)
    
    # Трекинг веток бота: {discussion_msg_id: count_replies}
    bot_threads = {}
    session_str = Path(SESSION_FILE).read_text() if Path(SESSION_FILE).exists() else ''

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.start(phone=PHONE, password=PASSWORD)
    
    Path(SESSION_FILE).write_text(client.session.save())
    print("Client started")

    if not TARGET_CHANNELS:
        print("No TARGET_CHANNEL in .env")
        return

    monitored_channels = {}
    for channel_username in TARGET_CHANNELS:
        entity = await client.get_entity(channel_username)
        full_channel = await client(GetFullChannelRequest(channel=entity))
        channel_id = str(entity.id)
        
        linked_chat_id = getattr(full_channel.full_chat, 'linked_chat_id', None)
        if linked_chat_id:
            monitored_channels[channel_id] = linked_chat_id
            print(f"Channel '{entity.title}' -> discussion {linked_chat_id}")

    @client.on(events.NewMessage)
    async def handler(event):
        message = event.message
        if not message.peer_id or not getattr(message.peer_id, 'channel_id', None):
            return

        message_channel_id = str(message.peer_id.channel_id)
        if message_channel_id not in monitored_channels:
            return

        discussion_group_id = monitored_channels[message_channel_id]
        
        # 1. Новый пост канала -> отвечаем в комментах
        if message.post:  # Это пост канала
            print(f"New channel post {message.id}")
            
            await rate_limiter.handle_comment()
            images = []
            if message.media:
                img_bytes = await download_media(client, message)
                if img_bytes:
                    images.append(media_to_base64(img_bytes))
                    print("Downloaded image for vision")
            
            comment = await generate_comment(message.text or "", images)
            print(f"Generated: {comment[:100]}...")

            discussion_message = await client(GetDiscussionMessageRequest(
                msg_id=message.id, peer=message.peer_id))
            discussion_msg_id = discussion_message.messages[0].id
            
            sent_msg = await client.send_message(
                discussion_group_id, comment, reply_to=discussion_msg_id)
            
            # Регистрируем новую ветку бота
            bot_threads[discussion_msg_id] = 1
            print(f"Bot reply #{bot_threads[discussion_msg_id]} to post {message.id} -> {sent_msg.id}")

        # 2. Ответ на сообщение бота в комментах
        elif (message.reply_to 
              and str(message.peer_id.channel_id) == str(discussion_group_id)
              and message.reply_to.reply_to_top_id in bot_threads):
            
            thread_id = message.reply_to.reply_to_top_id
            reply_count = bot_threads.get(thread_id, 0)
            
            if reply_count >= MAX_REPLIES_PER_THREAD:
                print(f"Thread {thread_id} limit reached ({reply_count}/{MAX_REPLIES_PER_THREAD})")
                return
            
            print(f"Reply to bot thread {thread_id} (#{reply_count + 1})")
            
            await rate_limiter.handle_comment()
            
            # Собираем контекст: текст + фото из ответа
            images = []
            reply_msg = await client.get_messages(discussion_group_id, ids=message.id)
            if reply_msg[0].media:
                img_bytes = await download_media(client, reply_msg[0])
                if img_bytes:
                    images.append(media_to_base64(img_bytes))
            
            comment = await generate_comment(
                reply_msg[0].text or "", 
                images, 
                reply_context=f"Предыдущий твой коммент в ветке"
            )
            
            sent_msg = await client.send_message(
                discussion_group_id, comment, reply_to=message.id)
            
            bot_threads[thread_id] = reply_count + 1
            print(f"Bot reply #{bot_threads[thread_id]} in thread {thread_id}")

    print("Bot running... Ctrl+C to stop")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())

