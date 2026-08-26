# چت‌بات هوش مصنوعی روی اکانت شخصی بله

MVP کوچک و قابل اجرا روی سرور: پیام خصوصی در بله → Gemini → پاسخ از همان اکانت شخصی.
لایهٔ ارتباط با بله `BaleClient==1.0.9` است — یک کلاینت کاربر (نه Bot API) که خودش
fork/rework پروژهٔ aiobale است. نسخه عمداً پین شده و سورس همان انتشار پیش از پیاده‌سازی
خط‌به‌خط بررسی شده است؛ خلاصهٔ آنچه از سورس تأیید شد در بخش ۲ آمده.

بدون Bot Token. بدون polling. بدون Redis/Celery/RAG/پنل وب.

---

## ۱. ساختار پروژه

```
bale-ai-userbot/
├── README.md              ← همین فایل
├── pyproject.toml         ← وابستگی‌ها (uv)
├── requirements.txt       ← همان وابستگی‌ها برای pip
├── .env.example           ← تمام تنظیمات؛ کپی کن به .env
├── .gitignore             ← session، دیتابیس و .env هرگز کامیت نمی‌شوند
├── login.py               ← ورود یک‌بارهٔ تعاملی (شماره → OTP → session.bale)
├── run.py                 ← اجرای ربات
├── balebot/
│   ├── config.py          ← خواندن تنظیمات از env (هیچ چیز hard-code نیست)
│   ├── logging_setup.py   ← لاگ
│   ├── storage.py         ← تاریخچهٔ گفتگو در SQLite
│   ├── llm.py             ← Gemini (پشت یک اینترفیس کوچک)
│   ├── client.py          ← زیرکلاس Client برای اصلاح باگ echo خودِ کاربر
│   ├── handlers.py        ← فیلتر، حافظه، صدا زدن مدل، ارسال پاسخ
│   └── app.py             ← سیم‌کشی همه‌چیز + lifespan
├── examples/echo.py       ← نمونهٔ اکو با import درست (baleclient، نه aiobale)
└── tests/                 ← ۱۸ تست، بدون نیاز به شبکه یا اکانت واقعی
```

جریان داده:

```
Bale (WebSocket)
  → session._listen()        [BaleClient]
  → client.handle_update()   [ChatClient: echo خودمان دور ریخته می‌شود]
  → dispatcher.dispatch("message", ...)
  → IsText()                 [فیلتر]
  → MessageHandler           [خصوصی؟ خودم نیستم؟ در allowlist هست؟]
  → ConversationStore.history()  → Gemini → ConversationStore.append()
  → message.answer()
```

---

## ۲. آنچه از سورس BaleClient تأیید شد (قبل از پیاده‌سازی)

| موضوع | واقعیت در نسخهٔ 1.0.9 |
|---|---|
| ورود | `Client.start()` → `_ensure_token_exists()` → اگر session نبود `PhoneLoginCLI` تعاملی |
| Session | فایل `*.bale`؛ اگر پسوند ندهی خودش `.bale` می‌گذارد و `resolve()` می‌کند |
| رویداد | `session._listen()` → `handle_update()` → `dispatcher.dispatch(event_type, event, client=self)` |
| هندلر | `@dp.message(*filters)`؛ اولین هندلری که فیلترهایش پاس شود اجرا و بقیه رها می‌شوند |
| پاسخ | `Message.answer(text)` / `Message.reply(text)` / `client.send_message(text, chat_id, chat_type)` |
| تایپینگ | `client.start_typing(chat_id, chat_type)` / `stop_typing(...)` |
| Reconnect | حلقهٔ `while not self._stopped` در `start()` + `_ping_loop()` هر ۵ ثانیه |
| فیلترها | `IsPrivate`, `IsText`, `IsDocument`, `IsGift`, `ChatTypeFilter`, `RegexFilter`, `F` |

سه نکتهٔ ریز اما تعیین‌کننده که در کد لحاظ شده‌اند:

1. **`_should_ignore` باگ دارد.** در `client.py:662` نوشته شده `self._ignored_messages.targets.remove()`
   بدون آرگومان → برای هر echo از پیام‌های خودمان `TypeError` می‌دهد، و لیست `targets`
   هیچ‌وقت خالی نمی‌شود. `balebot/client.py` این متد را درست override می‌کند
   (حذف درست + سقف ۵۱۲ آیدی). علاوه بر آن، هندلر مستقل هم چک می‌کند
   `message.sender_id == client.id` باشد یا نه — دو لایه محافظت در برابر لوپ.
2. **هندلر باید `async def` باشد، نه یک شیء صدازدنی.** دیسپچر با
   `inspect.iscoroutinefunction` تصمیم می‌گیرد؛ برای instance این تابع `False`
   برمی‌گرداند و هندلر داخل thread executor بدون await رها می‌شود. به همین دلیل
   `build_router` یک `async def` ثبت می‌کند که به شیء هندلر delegate می‌کند.
3. **آپدیت‌ها به شکل task موازی dispatch می‌شوند** (`asyncio.create_task` در `aiohttp.py:77`)
   و استثناها بی‌صدا بلعیده می‌شوند. پس هندلر خودش `try/except` سراسری دارد و برای هر چت
   یک `asyncio.Lock` می‌گیرد تا ترتیب گفتگو حفظ شود.

دربارهٔ خواستهٔ «اصلاح import های قدیمی aiobale»: در خود پکیج نصب‌شده هیچ `import aiobale`ای
نیست؛ فقط فایل‌های `examples/` مخزن (به‌جز `magazine.py`) هنوز `aiobale` را import می‌کنند.
نمونهٔ درست در `examples/echo.py` همین پروژه آمده است. سورس BaleClient دست‌نخورده می‌ماند
(به‌عنوان dependency نصب می‌شود)، و تنها اصلاح رفتاری‌اش در `balebot/client.py` به شکل
زیرکلاس انجام شده تا ارتقای نسخه ساده بماند.

---

## ۳. وابستگی‌ها

```
BaleClient==1.0.9      # لایهٔ بله (خودش aiohttp, pydantic, magic-filter, blackboxprotobuf می‌آورد)
google-genai>=1.20,<3  # Gemini
python-dotenv>=1.0     # خواندن .env
```
Python 3.11+ الزامی است (خودِ BaleClient این را می‌خواهد). SQLite از کتابخانهٔ استاندارد
استفاده می‌شود؛ هیچ ORM یا درایور اضافه‌ای نصب نمی‌شود.

برای تست: `pytest`, `pytest-asyncio`.

---

## ۴. راه‌اندازی

```bash
git clone https://github.com/alighaffari3000/bale-ai-userbot.git
cd bale-ai-userbot

# ۱) محیط و وابستگی‌ها
uv venv --python 3.11
uv pip install -r requirements.txt
# یا: python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt

# ۲) تنظیمات
cp .env.example .env
$EDITOR .env          # حداقل GEMINI_API_KEY و SYSTEM_PROMPT

# ۳) ورود یک‌بارهٔ اکانت شخصی (شماره → کد پیامکی)
.venv/bin/python login.py
#   شماره را بدون + و به‌صورت 98XXXXXXXXXX وارد کن
#   خروجی: data/session.bale با دسترسی 0600

# ۴) اجرا
.venv/bin/python run.py
```

اجرای دوم به بعد دیگر OTP نمی‌خواهد؛ توکن از `data/session.bale` خوانده می‌شود.

اجرای دائمی روی VPS (systemd):

```ini
# /etc/systemd/system/bale-chatbot.service
[Unit]
Description=Bale AI chatbot
After=network-online.target

[Service]
User=balebot
WorkingDirectory=/opt/bale-ai-userbot
ExecStart=/opt/bale-ai-userbot/.venv/bin/python run.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`Restart=always` تنها تور ایمنی بیرونی است؛ قطع و وصل شدن شبکه را خودِ BaleClient
داخل `start()` مدیریت می‌کند (cleanup → ۵ ثانیه صبر → connect → handshake).

---

## ۵. تنظیمات (همه در `.env`)

| کلید | پیش‌فرض | توضیح |
|---|---|---|
| `GEMINI_API_KEY` | — | الزامی |
| `GEMINI_MODEL` | `gemini-2.5-flash` | مدل |
| `LLM_TEMPERATURE` / `LLM_MAX_OUTPUT_TOKENS` | `0.7` / `1024` | |
| `SYSTEM_PROMPT` / `SYSTEM_PROMPT_FILE` | متن پیش‌فرض | پرامپت سیستمی (فایل اولویت دارد) |
| `BALE_SESSION_FILE` | `./data/session.bale` | محل توکن اکانت |
| `BALE_PROXY` | — | پراکسی خروجی |
| `BALE_ALLOWED_USER_IDS` | خالی | فقط به این آیدی‌ها جواب بده |
| `REPLY_MODE` | `answer` | `answer` یا `reply` (نقل‌قول‌دار) |
| `TYPING_INDICATOR` | `true` | نمایش «در حال نوشتن» |
| `HANDLE_GROUPS` | `false` | جواب دادن در گروه/کانال |
| `DB_PATH` | `./data/chat.db` | |
| `MAX_HISTORY_TURNS` | `12` | تعداد رفت‌وبرگشت‌هایی که به مدل داده می‌شود |
| `MAX_INPUT_CHARS` / `MAX_REPLY_CHARS` | `4000` / `3500` | ورودی بلند رد، پاسخ بلند تکه‌تکه می‌شود |
| `LOG_LEVEL` / `LOG_MESSAGE_TEXT` | `INFO` / `false` | متن پیام‌ها به‌صورت پیش‌فرض لاگ نمی‌شود |

دستورهای درون چت: `/help` و `/reset` (پاک کردن حافظهٔ همان چت).

---

## ۶. تست

```bash
.venv/bin/python -m pytest tests -q     # 18 passed
```
تست‌ها شبکه یا اکانت واقعی نمی‌خواهند: `tests/test_handlers.py` منطق گیت‌کیپینگ،
حافظه و خطای مدل را می‌سنجد و `tests/test_dispatch_integration.py` مسیر واقعی
`Dispatcher → Router → filter → handler → answer` را با اشیای واقعی `Message`
و همچنین اصلاح `_should_ignore` را بررسی می‌کند.

---

## ۷. ملاحظات امنیتی

- **`session.bale` عملاً رمز عبور اکانت است.** JWT کامل اکانت داخلش است؛ هرکس آن را
  داشته باشد همان اکانت است. در git نرود (`.gitignore` هم `data/` و هم `*.bale` و هم
  `.env` را کنار می‌گذارد)، در ایمیج داکر عمومی نرود، در بکاپ عمومی نرود.
  `prepare_session_file()` هنگام هر اجرا مجوز فایل را روی `0600` تنظیم می‌کند.
- **کلید Gemini** فقط از env/`.env` خوانده می‌شود و هرگز لاگ نمی‌شود.
- **محتوای چت داده‌ای شخصی است.** `LOG_MESSAGE_TEXT=false` پیش‌فرض است؛ متن پیام‌ها
  فقط با روشن کردن صریح آن لاگ می‌شوند. متن پیام‌ها در `chat.db` بدون رمزنگاری ذخیره
  می‌شود — فایل را روی دیسک رمزگذاری‌شده/با دسترسی محدود نگه دار و برای پاک‌سازی
  دوره‌ای `/reset` یا حذف رکوردها را در نظر بگیر.
- **allowlist را روشن کن** اگر ربات فقط برای چند نفر است (`BALE_ALLOWED_USER_IDS`).
  در غیر این صورت هرکسی که به شمارهٔ شما پیام بدهد به بودجهٔ Gemini شما دسترسی دارد.
- **گروه‌ها پیش‌فرض خاموش‌اند.** اکانتی که به همهٔ پیام‌های گروه جواب بدهد سریع
  ریپورت و مسدود می‌شود.
- **پارامترهای device در `start_phone_auth`** (`device_title`، `device_hash`، `api_key`،
  `app_id`) دست‌نخورده باقی مانده‌اند؛ تغییرشان بدون دلیل مشخص فقط ریسک بلاک‌شدن دارد.

---

## ۸. محدودیت‌های شناخته‌شده

**ناشی از غیررسمی بودن API بله:**
- BaleClient با API داخلی بله (WebSocket + protobuf) کار می‌کند، نه Bot API رسمی.
  این API بدون اطلاع قبلی تغییر می‌کند؛ هر تغییری می‌تواند parse را بشکند. نسخهٔ
  `BaleClient` را پین نگه دار و قبل از ارتقا تست کن.
- استفادهٔ خودکار از اکانت شخصی می‌تواند خلاف قواعد سرویس تلقی شود و به تعلیق یا
  مسدودی دائم اکانت منجر شود. ریت‌لیمیت را رعایت کن، اسپم نکن.
- خودِ کتابخانه هنوز کوچک و کم‌آزمون است (چند ستاره، مستندات ناقص). آن را
  «یک کلاینت reverse-engineered قابل استفاده» فرض کن، نه SDK پایدار.

**محدودیت‌های خودِ این MVP:**
- فقط پیام متنی. عکس/فایل/ویس دریافتی نادیده گرفته می‌شوند (فرستادنشان در BaleClient
  هست، اما در این MVP سیم‌کشی نشده).
- یک اکانت در هر پروسه. برای چند اکانت، چند پروسه با `BALE_SESSION_FILE` جدا.
- حافظه فقط بازپخش N پیام آخر همان چت است؛ نه خلاصه‌سازی، نه vector DB، نه RAG.
- هر چت هم‌زمان فقط یک پرسش را پردازش می‌کند؛ پیام دوم در حین پردازش، پیام
  «مشغولم» می‌گیرد (به‌جای صف‌بندی).
- `login.py` تعاملی است و باید یک‌بار روی ترمینال اجرا شود؛ روی سرور بی‌ترمینال،
  فایل session را از یک ماشین امن منتقل کن (با scp، نه از طریق git).
- در BaleClient 1.0.9 رویدادهای غیر از `message` (ویرایش/حذف پیام و...) در متد
  اصلی `_should_ignore` همیشه دور ریخته می‌شوند. زیرکلاس ما این را هم درست می‌کند،
  ولی این MVP عمداً هیچ هندلری برای آن‌ها ثبت نمی‌کند.
