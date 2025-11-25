import os
import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaPoll, MessageMediaWebPage
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest
from dotenv import load_dotenv
import aiohttp
from pathlib import Path
from datetime import datetime, timedelta

# Загрузка переменных окружения
load_dotenv()

API_ID = int(os.getenv('TELEGRAM_API_ID'))
API_HASH = os.getenv('TELEGRAM_API_HASH')
PHONE = os.getenv('TELEGRAM_PHONE_NUMBER')
PASSWORD = os.getenv('TELEGRAM_PASSWORD')
TARGET_CHANNELS = [ch.strip() for ch in os.getenv('TARGET_CHANNEL', '').split(',') if ch.strip()]
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_API_URL = os.getenv('OPENROUTER_API_URL')
MODEL_NAME = os.getenv('MODEL_NAME')

SESSION_FILE = 'telegram_session.session'
TEMP_DIR = Path('./temp')
TEMP_DIR.mkdir(exist_ok=True)

MAX_COMMENTS_PER_HOUR = 8
MAX_COMMENTS_PER_DAY = 50

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
        
        print("Waiting 30 seconds before replying...")
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

async def generate_comment(content, media_descriptions=[]):
    if not content.strip():
        content = "[Post contains media without text]"
    if media_descriptions:
        content += "\n\nMedia in post: " + ", ".join(media_descriptions)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENROUTER_API_KEY}"
    }

    system_msg = (
        "Ты — персонаж Саске Учиха из аниме \"Наруто\". "
        "Отвечай холодно, сдержанно, немного высокомерно и загадочно, как он. "
        "Используй стиль речи Саске: короткие, чёткие фразы с запахом внутренней борьбы и силы. "
        "Будь иногда саркастичным и остроумным, не прямолинейным."
    )

    data = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": f'Ответь на это сообщение как Саске: "{content}"'}
        ]
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(f"{OPENROUTER_API_URL}/chat/completions", headers=headers, json=data) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    return res['choices'][0]['message']['content']
                else:
                    print(f"OpenAI API error: {resp.status}")
                    return "That's interesting! Thanks for sharing."
        except Exception as e:
            print(f"Error generating comment with OpenAI: {e}")
            return "That's interesting! Thanks for sharing."

def extract_media_descriptions(message):
    descriptions = []
    media = message.media
    if media:
        if isinstance(media, MessageMediaPhoto):
            descriptions.append("Photo")
        elif isinstance(media, MessageMediaDocument):
            doc = media.document
            file_name = None
            video = False
            for attr in doc.attributes:
                if attr.__class__.__name__ == "DocumentAttributeFilename":
                    file_name = attr.file_name
                elif attr.__class__.__name__ == "DocumentAttributeVideo":
                    video = True
            if video:
                descriptions.append(f"Video: {file_name or 'video'}")
            else:
                descriptions.append(f"Document: {file_name or 'document'}")
        elif isinstance(media, MessageMediaPoll):
            descriptions.append(f'Poll: "{media.poll.question}"')
        elif isinstance(media, MessageMediaWebPage):
            web = media.webpage
            if hasattr(web, 'title') and web.title:
                descriptions.append(f'Webpage: "{web.title}"')
            elif hasattr(web, 'url') and web.url:
                descriptions.append(f'Webpage: {web.url}')
            else:
                descriptions.append("Webpage")
        else:
            descriptions.append(f"Media of type: {media.__class__.__name__}")
    return descriptions

async def main():
    print("Starting Safe Telegram Auto Commenter...")
    print(f"Rate limits: {MAX_COMMENTS_PER_HOUR} comments per hour, {MAX_COMMENTS_PER_DAY} comments per day")

    if not PASSWORD:
        raise Exception("TELEGRAM_PASSWORD is not set in environment variables")

    rate_limiter = RateLimiter(MAX_COMMENTS_PER_HOUR, MAX_COMMENTS_PER_DAY, True)

    session_str = ''
    if Path(SESSION_FILE).exists():
        session_str = Path(SESSION_FILE).read_text()

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)

    # Старт клиента с подпиской на события
    await client.start(phone=PHONE, password=PASSWORD)
    print("Telegram client started")

    # Сохраняем сессию после авторизации
    new_session_str = client.session.save()
    Path(SESSION_FILE).write_text(new_session_str)

    if not TARGET_CHANNELS:
        print("TARGET_CHANNEL environment variable is empty or not set. Please add channel usernames separated by commas.")
        return

    print(f"Found {len(TARGET_CHANNELS)} channels to monitor: {TARGET_CHANNELS}")

    monitored_channels = {}

    for channel_username in TARGET_CHANNELS:
        try:
            print(f"Resolving channel: {channel_username}")
            entity = await client.get_entity(channel_username)
            full_channel = await client(GetFullChannelRequest(channel=entity))
            channel_id = str(entity.id)

            linked_chat_id = getattr(full_channel.full_chat, 'linked_chat_id', None)
            if linked_chat_id:
                monitored_channels[channel_id] = linked_chat_id
                print(f"Will comment in group {linked_chat_id} for channel '{entity.title}'")
            elif getattr(entity, 'megagroup', False):
                monitored_channels[channel_id] = entity.id
                print(f"No linked chat found. Posting comments directly to group '{entity.title}'")
            else:
                print(f"Channel '{entity.title}' has no comment section. Skipping.")
        except Exception as e:
            print(f"Could not resolve channel '{channel_username}': {e}. Skipping.")

    if not monitored_channels:
        print("Fatal: No channels with discussion groups found. Check .env channel usernames.")
        return

    @client.on(events.NewMessage)
    async def handler(event):
        message = event.message
        if not message or not message.peer_id or not getattr(message.peer_id, 'channel_id', None):
            return

        message_channel_id = str(message.peer_id.channel_id)
        if message_channel_id in monitored_channels:
            discussion_group_id = monitored_channels[message_channel_id]

            print(f"New post in monitored channel {message_channel_id} at {datetime.now().isoformat()}")
            text_preview = (message.text[:70] + "...") if message.text else "[No Text]"
            print(f"Post content: {text_preview}")

            media_desc = extract_media_descriptions(message)
            if media_desc:
                print(f"Post contains media: {', '.join(media_desc)}")

            await rate_limiter.handle_comment()
            comment = await generate_comment(message.text or "", media_desc)
            print(f"Generated comment: {comment}")

            try:
                discussion_message = await client(GetDiscussionMessageRequest(msg_id=message.id))
                discussion_msg_id = discussion_message.messages[0].id
                await client.send_message(discussion_group_id, comment, reply_to=discussion_msg_id)
                
                # await client.send_message(discussion_group_id, comment, reply_to=message.id)
                print(f"Comment posted successfully to discussion group {discussion_group_id}.")
            except Exception as e:
                print(f"Error posting comment: {e}")
                if rate_limiter.hour_history:
                    rate_limiter.hour_history.pop()
                if rate_limiter.day_history:
                    rate_limiter.day_history.pop()

    print("Bot is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())

