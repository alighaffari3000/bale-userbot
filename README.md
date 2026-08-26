# balekit

زیرساخت کار با **اکانت شخصی بله** روی پایتون. یک لایهٔ نازک روی
[`BaleClient`](https://pypi.org/project/BaleClient/) که ورود/نشست، حلقهٔ رویداد،
تشخیص نوع پیام و ارسال/دریافت هر نوع فایل را یک‌دست می‌کند.

این یک ربات یا محصول نیست: هیچ منطق کاربردی، هیچ مدل هوش مصنوعی و هیچ پایگاه‌دادهٔ
درون‌ساختی ندارد. کاری که با پیام‌ها می‌کنی، کار برنامهٔ توست.

بدون Bot Token — با شمارهٔ خودت لاگین می‌کنی و پیام‌ها از همان اکانت شخصی می‌روند
و می‌آیند.

```python
from balekit import BaleApp, Config, MessageKind

app = BaleApp(Config.from_env())

@app.on_message(kinds=[MessageKind.PHOTO, MessageKind.VIDEO])
async def save_media(message, client):
    path = await app.download(message)          # هر نوع فایلی، یک متد
    await message.reply(f"saved {path.name}")

app.run()                                        # اتصال، هندشیک، reconnect
```

---

## چرا یک لایه روی BaleClient؟

`BaleClient` کار سنگین پروتکل را انجام می‌دهد. اما موقع ساختن روی آن، هر پروژه
مجبور است این پنج چیز را از نو بنویسد — و سه‌تای اول باگ‌های واقعی کتابخانه‌اند
که در همین ریپو تست دارند:

| مشکل در `BaleClient 1.0.9` | چه بلایی سرت می‌آورد | راه‌حل در balekit |
|---|---|---|
| `Client._should_ignore` متد `list.remove()` را بدون آرگومان صدا می‌زند | هر echo از پیام‌های خودت `TypeError` می‌دهد و لیست pending هیچ‌وقت خالی نمی‌شود (نشت حافظه) | `KitClient` متد را درست override می‌کند + سقف ۵۱۲ آیدی |
| همان متد برای رویدادهای غیرِ `message` همیشه «نادیده بگیر» برمی‌گرداند | ویرایش/حذف پیام و بقیهٔ رویدادها هرگز dispatch نمی‌شوند | همان override |
| `AudioExt(album=...)` تگ‌ها را می‌اندازد (validatorِ before کلیدهای aliasی را با `None` بازنویسی می‌کند) — و `send_audio` دقیقاً همین کار را می‌کند | آلبوم/ژانر/نام قطعه بی‌صدا گم می‌شوند | `audio_ext()` از طریق aliasها می‌سازد؛ `send_media` وقتی تگ داری از مسیر امن می‌رود |
| دیسپچر با `inspect.iscoroutinefunction` تصمیم می‌گیرد | هندلری که یک شیء صدازدنی است (نه تابع) داخل thread executor بدون await رها می‌شود | `wrap_handler` همیشه یک coroutine function ثبت می‌کند |
| آپدیت‌ها با `asyncio.create_task` پرتاب می‌شوند | استثنای هندلر همراه task بی‌صدا گم می‌شود | `wrap_handler` لاگ می‌کند و اختیاری `on_error` صدا می‌زند |

به‌علاوهٔ چیزی که اصلاً وجود ندارد: **یک واژگان واحد برای «این پیام چیست؟»**.
در پروتکل بله عکس، ویدئو، ویس، موزیک، گیف و فایل ساده همگی یک `DocumentMessage`
هستند و فقط `document.ext` و MIME از هم جدایشان می‌کند — و اگر پیام کیبورد اینلاین
داشته باشد، یک لایه هم داخل `content.bot_message` فرو می‌رود. `describe()` همهٔ این
را صاف می‌کند.

---

## پشتیبانی از فرمت‌ها

| نوع | دریافت (`describe`) | ارسال (`send_media`) | کپی بدون آپلود (`resend`) |
|---|---|---|---|
| متن | ✅ `MessageKind.TEXT` | `client.send_message` | — |
| عکس | ✅ `PHOTO` (+ابعاد، thumbnail) | ✅ (ابعاد و کاور خودکار با Pillow) | ✅ |
| ویدئو | ✅ `VIDEO` (+ابعاد، مدت) | ✅ | ✅ |
| گیف | ✅ `GIF` | ✅ | ✅ |
| پیام صوتی | ✅ `VOICE` (+مدت) | ✅ | ✅ |
| موزیک | ✅ `AUDIO` (+مدت، آلبوم/ژانر/ترک) | ✅ (تگ‌ها حفظ می‌شوند) | ✅ |
| فایل | ✅ `DOCUMENT` (+نام، MIME، حجم) | ✅ | ✅ |
| کپشن روی هر مدیا | ✅ | ✅ | ✅ |
| کیبورد اینلاین | ✅ (`has_keyboard`) | ✅ (`reply_markup`) | — |
| فوروارد | ✅ `FORWARD` | — | — |
| پیام سرویس | ✅ `SERVICE` (+متن) | — | — |
| بستهٔ هدیه | ✅ `GIFT` | `client.send_gift` | — |

**لوکیشن، مخاطب (contact card) و استیکر پشتیبانی نمی‌شوند** — نه در balekit و نه
در `BaleClient 1.0.9`. در `MessageContent` این نسخه فقط `document`، `text`،
`service_message`، `bot_message` و `gift` وجود دارد؛ هیچ فیلد geo/contact/sticker
در پروتکلِ پیاده‌سازی‌شده نیست. اگر لازمشان داری، باید اول در خودِ کتابخانه (یا یک
فورک) به پروتکل اضافه شوند؛ balekit چیزی را که لایهٔ زیرین ندارد نمی‌تواند بسازد.

---

## نصب

```bash
pip install -r requirements.txt        # یا: uv pip install -e ".[media]"
pip install Pillow                     # اختیاری: ابعاد و thumbnail عکس
```

Python 3.11+ (الزام خود `BaleClient`).

## ورود (یک‌بار)

```bash
cp .env.example .env
python -m balekit login                # شماره → کد پیامکی → data/session.bale
python -m balekit whoami               # نشست ذخیره‌شده مال کدام اکانت است
```

از این به بعد نشست از فایل خوانده می‌شود و OTP لازم نیست.

---

## API

**`BaleApp`** — اجرا و ثبت هندلر:

```python
app = BaleApp(Config.from_env())

@app.on_message(kinds=[MessageKind.TEXT])          # فیلتر بر اساس نوع
async def on_text(message, client): ...

@app.on_message(IsMedia(), FromUsers(123, 456))     # فیلترهای دلخواه
async def on_media(message, client): ...

app.run()            # مسدودکننده
await app.start()    # داخل event loop خودت
```

گیت‌های سطح config (خصوصی/گروه، allowlist، پیام‌های خودت) قبل از فیلترهای تو
اعمال می‌شوند.

**`describe(message) -> MessageInfo`** — نمای صاف پیام:
`kind`, `text`, `caption`, `body`, `media`, `has_keyboard`, `is_forward`,
`reply_to_id`, `sender_id`, `chat_id`, `chat_type`, `service_text`.
و `MediaInfo`: `file_id`, `access_hash`, `mime_type`, `name`, `size`,
`width`, `height`, `duration`, `has_thumb`, `album`, `genre`, `track`.

**ارسال و دریافت:**

```python
await app.send("photo.jpg", chat_id)                 # نوع از روی فایل تشخیص داده می‌شود
await app.send(raw_bytes, chat_id, name="a.mp3", album="Album")
await app.reply_with(message, "doc.pdf", caption="…")
await app.resend(message, other_chat_id)             # کپی بدون دانلود/آپلود
data  = await app.download(message, destination=None)   # bytes
path  = await app.download(message)                     # فایل در پوشهٔ دانلود
```

**فیلترها:** `Kind(...)`, `IsMedia()`, `NotSelf()`, `FromUsers(...)`,
`InChats(...)`, `ChatScope(private=, groups=)` — کنار فیلترهای خود
`baleclient.filters` و `F` قابل استفاده‌اند.

هرجا لازم شد، `app.client` همان `Client` کامل `BaleClient` است: هیچ چیزی از
کتابخانهٔ زیرین پنهان نشده.

---

## تنظیمات

همه از `.env` یا محیط: `BALE_SESSION_FILE`, `BALE_PROXY`, `BALE_DOWNLOAD_DIR`,
`HANDLE_PRIVATE`, `HANDLE_GROUPS`, `BALE_ALLOWED_USER_IDS`, `IGNORE_SELF`,
`SERIALIZE_PER_CHAT`, `LOG_LEVEL`, `LOG_MESSAGE_TEXT`. جزئیات در `.env.example`.

## نمونه‌ها

- `examples/echo_any.py` — هر چیزی که آمد را برمی‌گرداند (مدیا بدون آپلود مجدد)
- `examples/inspect_messages.py` — چاپ یک‌خطی هر پیام ورودی
- `examples/download_media.py` — ذخیرهٔ هر فایل دریافتی

---

## تست

```bash
python -m pytest tests -q      # 76 تست، بدون شبکه و بدون اکانت
```

تست‌ها با اشیای واقعی `baleclient.types` ساخته می‌شوند (نه mock پروتکل): هر شکل
محتوا یک‌بار از مسیر `Dispatcher → filter → handler` عبور داده می‌شود. دو تست
عمداً باگ‌های بالادست را assert می‌کنند؛ اگر روزی fail شدند یعنی `BaleClient`
درستشان کرده و می‌شود workaround را حذف کرد.

**تست زنده** (نیازمند اکانت واقعی — در CI اجرا نمی‌شود):

```bash
python tools/live_check.py            # به «پیام‌های ذخیره‌شده»ی خودت
python tools/live_check.py --chat ID --keep
```

هر نوع پیام را می‌فرستد، از history می‌خواند، دوباره classify می‌کند، یکی را
دانلود می‌کند و در پایان پیام‌های خودش را پاک می‌کند.

---

## امنیت

- `session.bale` عملاً رمز عبور اکانت است. در git، ایمیج داکر یا بکاپ عمومی نرود.
  `.gitignore` هم `data/` و هم `*.bale` و هم `.env` را کنار می‌گذارد و
  `prepare_session_file()` مجوز فایل را روی `0600` می‌گذارد.
- متن پیام‌ها به‌صورت پیش‌فرض لاگ نمی‌شود (`LOG_MESSAGE_TEXT=false`).
- اگر سرویس فقط برای چند نفر است، `BALE_ALLOWED_USER_IDS` را پر کن.
- گروه‌ها پیش‌فرض خاموش‌اند؛ اکانتی که به هر پیام گروه واکنش نشان دهد سریع ریپورت
  می‌شود.
- پارامترهای device در `start_phone_auth` دست‌نخورده رها شده‌اند؛ تغییرشان بدون
  دلیل مشخص فقط ریسک بلاک است.

## محدودیت‌ها

- API بله **غیررسمی و داخلی** است (WebSocket + protobuf)، نه Bot API. می‌تواند
  بدون اطلاع تغییر کند؛ به همین دلیل نسخهٔ `BaleClient` پین شده است.
- اتوماسیون روی اکانت شخصی ممکن است خلاف قواعد سرویس تلقی شود؛ ریسک تعلیق
  اکانت واقعی است.
- لوکیشن/مخاطب/استیکر: پشتیبانی نمی‌شود (بالاتر توضیح داده شد).
- ابعاد ویدئو و مدت زمان به‌صورت خودکار استخراج نمی‌شوند (نیازمند ffprobe است)؛
  اگر می‌خواهی پیش‌نمایش درست باشد خودت `width`/`height`/`duration` را بده.
- `MessageKind.FORWARD` یعنی «استاب خالی»؛ محتوای اصلی فوروارد در پیام نقل‌شده
  (`message.replied_to`) است.
