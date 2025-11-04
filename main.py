
import os, re, asyncio, tempfile, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import CommandStart

import vk_api
from vk_api.upload import VkUpload

# ----------------------- ENV -----------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
VK_GROUP_ID = int(os.getenv("VK_GROUP_ID", "0"))        # ID сообщества БЕЗ минуса
VK_TOKEN = os.getenv("VK_TOKEN", "")
VK_CATEGORY_ID = int(os.getenv("VK_CATEGORY_ID", "0"))  # дефолтная категория, если не указано в посте
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
POST_TIMES = os.getenv("POST_TIMES", "14:00,18:00")

# albums mapping: "бомберы:111111,куртки:222222"
ALBUMS = os.getenv("ALBUMS", "")

def parse_times(s: str):
    times = []
    for t in s.split(","):
        t = t.strip()
        if not t: continue
        h, m = t.split(":")
        times.append((int(h), int(m)))
    return times

def parse_albums_map(s: str):
    m = {}
    for pair in filter(None, [x.strip() for x in s.split(",")]):
        if ":" not in pair: 
            continue
        k, v = [p.strip() for p in pair.split(":", 1)]
        if v.isdigit():
            m[k.lower()] = int(v)
    return m

ALBUM_MAP = parse_albums_map(ALBUMS)

# ----------------------- TG / VK -----------------------
bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()
uploader = VkUpload(vk_session)

# ----------------------- Regex -----------------------
PRICE_RE = re.compile(r'(?i)(?:^|[\n\r])\s*(?:цена|price)\s*:\s*([\d\s.,]+)')
SIZES_RE = re.compile(r'(?i)(?:^|[\n\r])\s*(?:размеры|sizes)\s*:\s*(.+)')
SKU_RE   = re.compile(r'(?i)(?:^|[\n\r])\s*(?:артикул|sku)\s*:\s*([#\w\-]+)')

CAT_RE   = re.compile(r'(?i)(?:^|\n)\s*(?:категория|category|cat)\s*:\s*([#\w\- ]+)')
CAT_TAG  = re.compile(r'#cat[_\-]?(\d+)', re.I)

ALBUM_RE = re.compile(r'(?i)(?:^|\n)\s*(?:подборка|album|подкатегория)\s*:\s*([#\w ,\-]+)')
ALB_TAG  = re.compile(r'#alb[_\-]?(\d+)', re.I)

CATEGORY_MAP = {
    "men": 2, "муж": 2, "мужское": 2,
    "women": 1, "жен": 1, "женское": 1,
    "kids": 3, "дет": 3,
    "shoes": 4, "обув": 4, "bags": 4, "сумк": 4,
    "access": 5, "аксесс": 5,
}

# ----------------------- Parsing -----------------------
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

    # description
    desc_lines = []
    for ln in (text or "").splitlines()[1:]:
        low = ln.lower()
        if any(k in low for k in ["цена:", "price:", "размер", "sizes:", "артикул", "sku:", "категория:", "category:", "подборка:", "album:"]):
            continue
        desc_lines.append(ln)
    description = "\n".join(desc_lines).strip() or title

    # category
    category_id = None
    m = CAT_RE.search(text or "")
    if m:
        key = m.group(1).strip().lower()
        if key.isdigit():
            category_id = int(key)
        else:
            for k, v in CATEGORY_MAP.items():
                if k in key:
                    category_id = v
                    break
    m = CAT_TAG.search(text or "")
    if m:
        category_id = int(m.group(1))

    # albums
    album_ids = []
    m = ALBUM_RE.search(text or "")
    if m:
        tokens = [t.strip().lower() for t in re.split(r'[,\s]+', m.group(1)) if t.strip()]
        for t in tokens:
            if t.isdigit():
                album_ids.append(int(t))
            elif t in ALBUM_MAP:
                album_ids.append(ALBUM_MAP[t])
    for m in ALB_TAG.finditer(text or ""):
        album_ids.append(int(m.group(1)))

    return {"title": title, "price": price, "sizes": sizes, "sku": sku, "description": description,
            "category_id": category_id, "album_ids": album_ids}

def build_description_for_vk(p):
    parts = [p.get("description") or p["title"]]
    if p.get("sizes"): parts.append(f"Размеры: {p['sizes']}")
    if p.get("sku"): parts.append(f"Артикул: {p['sku']}")
    if p.get("price") is not None: parts.append(f"Цена: {int(p['price'])} ₽")
    return "\n".join(parts)

# ----------------------- Media helpers -----------------------
async def download_tg_file(file_id: str, bot_token: str) -> str:
    f = await bot.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{bot_token}/{f.file_path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.write(fd, r.content)
    os.close(fd)
    return path

def upload_market_main_photo(path: str) -> int:
    saved = uploader.photo_market(photos=path, group_id=VK_GROUP_ID, main_photo=True)
    return saved[0]["id"]

def upload_wall_photo(path: str) -> str:
    res = uploader.photo_wall([path], group_id=VK_GROUP_ID)
    p = res[0]
    return f"photo{p['owner_id']}_{p['id']}"

# ----------------------- VK actions -----------------------
def create_vk_product(data, main_photo_id: int):
    cat_id = data.get("category_id") or VK_CATEGORY_ID
    resp = vk.market.add(
        owner_id = -VK_GROUP_ID,
        name = data["title"][:100],
        description = build_description_for_vk(data),
        category_id = cat_id,
        price = int(data["price"] or 0),
        main_photo_id = main_photo_id,
        sku = data["sku"] or "",
        availability = 0
    )
    item_id = resp["market_item_id"]
    url = f"https://vk.com/market-{VK_GROUP_ID}?w=product-{VK_GROUP_ID}_{item_id}"
    return (-VK_GROUP_ID, item_id, url)

def add_to_albums(item_id: int, album_ids: list[int]):
    if not album_ids: return
    try:
        vk.market.addToAlbum(owner_id=-VK_GROUP_ID, item_id=item_id, album_ids=",".join(map(str, album_ids)))
    except Exception as e:
        print("addToAlbum error:", e)

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

def next_day_at(hour: int, minute: int, tzname: str) -> datetime:
    tz = ZoneInfo(tzname)
    now = datetime.now(tz)
    dt = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return dt

# ----------------------- Bot flow -----------------------
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Я создаю карточку товара ВК и делаю два отложенных поста на завтра.\n\n"
        "Формат (фото + текст):\n"
        "<code>MA-1 bomber\nЦена: 5500 ₽\nАртикул: MA1-BLK\nРазмеры: S/M/L/XL\nКатегория: men  # или 2, или #cat2\nПодборка: бомберы, #alb123456\n\nОписание...</code>"
    )

@dp.message(F.photo | F.caption | F.text)
async def handle_message(m: Message):
    text = m.caption or m.text or ""
    if not text.strip(): return
    product = parse_product(text)

    preview = (f"<b>{product['title']}</b>\n"
               f"Цена: {int(product['price']) if product['price'] is not None else '—'} ₽\n"
               f"{('Размеры: ' + (product['sizes'] or '')) if product.get('sizes') else ''}\n"
               f"{('Артикул: ' + product['sku']) if product.get('sku') else ''}\n"
               f"{('Категория: ' + str(product.get('category_id'))) if product.get('category_id') else ''}\n"
               f"{('Подборки: ' + ','.join(map(str, product.get('album_ids', [])))) if product.get('album_ids') else ''}\n\n"
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
        add_to_albums(item_id, product.get("album_ids") or [])
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

    urls = []
    for h, mnt in parse_times(POST_TIMES):
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
