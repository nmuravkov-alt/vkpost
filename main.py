
import os, re, asyncio, tempfile, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import CommandStart
from dateutil import parser as dateparser

import vk_api
from vk_api.upload import VkUpload

# ----------------------- ENV -----------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
VK_GROUP_ID = int(os.getenv("VK_GROUP_ID", "0"))        # ID сообщества БЕЗ минуса
VK_TOKEN = os.getenv("VK_TOKEN", "")
VK_CATEGORY_ID = int(os.getenv("VK_CATEGORY_ID", "0"))  # обязателен для market.add
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
POST_TIMES = os.getenv("POST_TIMES", "14:00,18:00")

if not BOT_TOKEN or not VK_TOKEN or not VK_GROUP_ID or not VK_CATEGORY_ID:
    print("❗Заполните .env: BOT_TOKEN, VK_TOKEN, VK_GROUP_ID, VK_CATEGORY_ID")

# ----------------------- TG -----------------------
bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# ----------------------- VK -----------------------
vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()
uploader = VkUpload(vk_session)

# ----------------------- Parsing -----------------------
PRICE_RE = re.compile(r'(?i)(?:^|[\n\r])\s*(?:цена|price)\s*:\s*([\d\s.,]+)')
SIZES_RE = re.compile(r'(?i)(?:^|[\n\r])\s*(?:размеры|sizes)\s*:\s*(.+)')
SKU_RE   = re.compile(r'(?i)(?:^|[\n\r])\s*(?:артикул|sku)\s*:\s*([#\w\-]+)')

def parse_product(text: str):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    title = lines[0] if lines else "Без названия"

    price = None
    m = PRICE_RE.search(text or "")
    if m:
        try:
            price = float(m.group(1).replace(" ", "").replace(",", "."))
        except:
            price = None

    sizes = None
    m = SIZES_RE.search(text or "")
    if m:
        sizes = ", ".join([s.strip() for s in re.split(r'[,\s/]+', m.group(1)) if s.strip()])

    sku = None
    m = SKU_RE.search(text or "")
    if m: sku = m.group(1).lstrip("#")

    # Описание: весь текст без первой строки и служебных полей
    desc_lines = []
    for ln in (text or "").splitlines()[1:]:
        low = ln.lower()
        if any(k in low for k in ["цена:", "price:", "размер", "sizes:", "артикул", "sku:"]):
            continue
        desc_lines.append(ln)
    description = "\n".join(desc_lines).strip() or title

    return {"title": title, "price": price, "sizes": sizes, "sku": sku, "description": description}

def build_description_for_vk(p):
    parts = [p.get("description") or p["title"]]
    if p.get("sizes"): parts.append(f"Размеры: {p['sizes']}")
    if p.get("sku"): parts.append(f"Артикул: {p['sku']}")
    if p.get("price") is not None: parts.append(f"Цена: {int(p['price'])} ₽")
    return "\n".join(parts)

# ----------------------- Helpers -----------------------
async def download_tg_file(file_id: str, bot_token: str) -> str:
    f = await bot.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{bot_token}/{f.file_path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.write(fd, r.content)
    os.close(fd)
    return path

def upload_market_main_photo(path: str) -> int:
    saved = uploader.photo_market(photos=path, group_id=VK_GROUP_ID, main_photo=True)
    return saved[0]["id"]

def create_vk_product(data, main_photo_id: int):
    resp = vk.market.add(
        owner_id = -VK_GROUP_ID,
        name = data["title"][:100],
        description = build_description_for_vk(data),
        category_id = VK_CATEGORY_ID,
        price = int(data["price"] or 0),
        main_photo_id = main_photo_id,
        sku = data["sku"] or "",
        availability = 0
    )
    item_id = resp["market_item_id"]
    url = f"https://vk.com/market-{VK_GROUP_ID}?w=product-{VK_GROUP_ID}_{item_id}"
    return (-VK_GROUP_ID, item_id, url)

def parse_times(s: str):
    times = []
    for t in s.split(","):
        t = t.strip()
        if not t: continue
        h, m = t.split(":")
        times.append((int(h), int(m)))
    return times

def next_day_at(hour: int, minute: int, tzname: str) -> datetime:
    tz = ZoneInfo(tzname)
    now = datetime.now(tz)
    dt = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return dt

def upload_wall_photo(path: str) -> str:
    res = uploader.photo_wall([path], group_id=VK_GROUP_ID)
    p = res[0]
    return f"photo{p['owner_id']}_{p['id']}"

def schedule_wall_post(message_text: str, when_dt: datetime, attachments: str = "") -> int:
    publish_ts = int(when_dt.timestamp())
    resp = vk.wall.post(
        owner_id = -VK_GROUP_ID,
        from_group = 1,
        message = message_text or "",
        attachments = attachments,
        publish_date = publish_ts
    )
    return resp.get("post_id")

# ----------------------- Bot flow -----------------------
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Я превращаю ваше сообщение (фото + текст) в товар ВК и делаю два отложенных поста на завтра в 14:00 и 18:00.\n\n"
        "Отправьте одно сообщение с фото и текстом вида:\n"
        "<code>MA-1 bomber\nЦена: 5500 ₽\nАртикул: MA1-BLK\nРазмеры: S/M/L/XL\n\nСостояние: новое</code>\n\n"
        "Потом выберите: «Создать товар ВК» или «Отложить посты ВК».")

@dp.message(F.photo | F.caption | F.text)
async def handle_message(m: Message):
    text = m.caption or m.text or ""
    if not text.strip():
        return
    product = parse_product(text)

    preview = (f"<b>{product['title']}</b>\n"
               f"Цена: {int(product['price']) if product['price'] is not None else '—'} ₽\n"
               f"{('Размеры: ' + product['sizes']) if product.get('sizes') else ''}\n"
               f"{('Артикул: ' + product['sku']) if product.get('sku') else ''}\n\n"
               f"{product['description']}")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Создать товар ВК", callback_data="vk:add"),
        InlineKeyboardButton(text="🕒 Отложить посты ВК", callback_data="vk:schedule")
    ]])
    await m.reply("Нашёл карточку:\n\n" + preview, reply_markup=kb)

@dp.callback_query(F.data == "vk:add")
async def cb_add_product(cq: CallbackQuery):
    src = cq.message.reply_to_message
    text = src.caption or src.text or ""
    product = parse_product(text)

    if not src.photo:
        await cq.answer("Нужно хотя бы одно фото.", show_alert=True)
        return

    largest = src.photo[-1].file_id
    path = await download_tg_file(largest, BOT_TOKEN)

    try:
        main_id = upload_market_main_photo(path)
        owner_id, item_id, url = create_vk_product(product, main_id)
    finally:
        try: os.remove(path)
        except: pass

    await cq.message.answer(f"✅ Товар создан: {url}")
    await cq.answer("Готово!")

@dp.callback_query(F.data == "vk:schedule")
async def cb_schedule_posts(cq: CallbackQuery):
    src = cq.message.reply_to_message
    text = src.caption or src.text or ""
    product = parse_product(text)

    post_text = f"{product['title']}\n{product['description']}"
    if product.get("price") is not None:
        post_text += f"\nЦена: {int(product['price'])} ₽"

    attachment = ""
    path = None
    if src.photo:
        largest = src.photo[-1].file_id
        path = await download_tg_file(largest, BOT_TOKEN)
        try:
            attachment = upload_wall_photo(path)
        finally:
            try: os.remove(path)
            except: pass

    times = parse_times(POST_TIMES)
    urls = []
    for h, mnt in times:
        when = next_day_at(h, mnt, TIMEZONE)
        post_id = schedule_wall_post(post_text, when, attachments=attachment)
        urls.append((when, f"https://vk.com/wall-{VK_GROUP_ID}_{post_id}"))

    text_urls = "\n".join([f"{when.strftime('%d.%m %H:%M')} — {u}" for when, u in urls])
    await cq.message.answer("✅ Два отложенных поста созданы:\n" + text_urls)
    await cq.answer("Запланировано!")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
