# bale-userbot

زیرساخت کار با **اکانت شخصی بله** روی پایتون. یک لایهٔ نازک روی
[`BaleClient`](https://pypi.org/project/BaleClient/) که ورود/نشست، حلقهٔ رویداد،
تشخیص نوع پیام و ارسال/دریافت هر نوع فایل را یک‌دست می‌کند.

این یک ربات یا محصول نیست: هیچ منطق کاربردی، هیچ مدل هوش مصنوعی و هیچ پایگاه‌دادهٔ
درون‌ساختی ندارد. کاری که با پیام‌ها می‌کنی، کار برنامهٔ توست.

بدون Bot Token — با شمارهٔ خودت لاگین می‌کنی و پیام‌ها از همان اکانت شخصی می‌روند
و می‌آیند.

```python
from bale_userbot import BaleApp, Config, MessageKind

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
مجبور است این‌ها را از نو بنویسد — و شش‌تای اول باگ‌های واقعی کتابخانه‌اند
که در همین ریپو تست دارند (اولی و دومی روی اکانت واقعی تأیید شده‌اند):

| مشکل در `BaleClient 1.0.9` | چه بلایی سرت می‌آورد | راه‌حل در bale-userbot |
|---|---|---|
| والیدیتور `_check_empty` در `MessageContent` فیلد متن (`"15"`) را همیشه null می‌کند و پرچم `empty` (`"5"`) را با صرفِ حضور true | **هر پیام متنی دریافتی** (history و رویدادها) بدون متن و به‌شکل forward/unknown پارس می‌شود — تأییدشده روی اکانت واقعی | `patches.apply_wire_fixes()` هنگام import والیدیتور را با نسخهٔ درست عوض و مدل‌ها را rebuild می‌کند |
| `Client._should_ignore` متد `list.remove()` را بدون آرگومان صدا می‌زند | هر echo از پیام‌های خودت `TypeError` می‌دهد و لیست pending هیچ‌وقت خالی نمی‌شود (نشت حافظه) | `KitClient` متد را درست override می‌کند + سقف ۵۱۲ آیدی |
| همان متد برای رویدادهای غیرِ `message` همیشه «نادیده بگیر» برمی‌گرداند | ویرایش/حذف پیام و بقیهٔ رویدادها هرگز dispatch نمی‌شوند | همان override |
| `AudioExt(album=...)` تگ‌ها را می‌اندازد (validatorِ before کلیدهای aliasی را با `None` بازنویسی می‌کند) — و `send_audio` دقیقاً همین کار را می‌کند | آلبوم/ژانر/نام قطعه بی‌صدا گم می‌شوند | `audio_ext()` از طریق aliasها می‌سازد؛ `send_media` وقتی تگ داری از مسیر امن می‌رود |
| `ServiceMessage` هر دو فیلد متن و ext را الزامی می‌داند و `Thumbnail` هم w/h را — ولی خودِ `DocumentMessage` تامبِ ناقص را نگه می‌دارد | یک فیلد غایب کل `MessageContent` را می‌ترکاند؛ روی وب‌سوکت این استثنا بلعیده می‌شود و **کل آپدیت بی‌صدا گم می‌شود** | همان پچ، بلوک ناقص را null می‌کند تا بقیهٔ پیام برسد |
| `set_reaction`/`remove_reaction`/`typing`/`stop_typing` با `Peer(type=chat_type)` خام ساخته می‌شوند، ولی `PeerType` فقط 0/1/2 دارد | ری‌اکشن یا «در حال تایپ» در ربات، سوپرگروه و کانال، *قبل از هر درخواست شبکه*، ValidationError می‌دهد | `react()` / `unreact()` / `set_typing()` peer را مثل بقیهٔ کتابخانه resolve می‌کنند |
| دیسپچر با `inspect.iscoroutinefunction` تصمیم می‌گیرد | هندلری که یک شیء صدازدنی است (نه تابع) داخل thread executor بدون await رها می‌شود | `wrap_handler` همیشه یک coroutine function ثبت می‌کند |
| آپدیت‌ها با `asyncio.create_task` پرتاب می‌شوند | استثنای هندلر همراه task بی‌صدا گم می‌شود | `wrap_handler` لاگ می‌کند و اختیاری `on_error` صدا می‌زند |
| در پاسخ `GetFullGroup` گروه‌ها، فیلد `3` هر عضو **نام نمایشی** (رشته) است ولی `Member.date` آن را `int` اعلام کرده | یک عضو اسم‌دار کل پاسخ را رد می‌کند — عنوان گروه هم با آن گم می‌شود؛ کانال‌ها (بدون لیست عضو) سالم‌اند | همان پچ مقدار را از فیلد `3` بیرون می‌برد ولی نامش را در `member.display_name` نگه می‌دارد؛ `MemberInfo.name` رایگان پر می‌شود |
| `CallableObject.call` امضای فیلتر را با `param.annotation.__name__` می‌خواند | هر فیلتری در ماژولی با `from __future__ import annotations` دیسپچر را روی اولین آپدیت می‌ترکاند | همان پچ، متد را با نسخهٔ `getattr`دار عوض می‌کند |
| مسیر HTTP در `session.post` نه `method_data` می‌دهد نه `context` — ولی `MessageResponse.add_message` هر دو را لازم دارد | هر ارسالی قبل از بالا آمدن وب‌سوکت (مثلاً داخل lifespan) `AttributeError` می‌دهد | همان پچ؛ بدون context پیام echo قابل بازسازی نیست، `message=None` برمی‌گردد |
| یک دیالوگ با پیام آخرِ بدشکل (کیبورد بات، سند ناقص) اعتبارسنجی `PeerData` را می‌ترکاند | **کل صفحهٔ `LoadDialogs`** می‌سوزد و فهرست چت بی‌صدا کوتاه می‌شود | همان پچ: هر دیالوگ جدا آزموده می‌شود؛ محتوای بدشکل حذف و peer حفظ می‌شود |
| `Client.load_members` وقتی offset نداری `StringValue(value="None")` می‌فرستد — رشتهٔ «None» به‌عنوان کِرسر | همان صفحهٔ اول هم با کِرسرِ بی‌معنی خواسته می‌شود | `groups.iter_members` مستقیم `LoadMembers` را صدا می‌زند و وقتی offset نیست فیلد را اصلاً نمی‌فرستد |
| `MembersResponse` فقط لیست اعضا را مدل کرده و کِرسرِ صفحهٔ بعد را دور می‌ریزد | صفحه‌بندی اعضا از بیرون غیرممکن است | کِرسر از `model_extra` خوانده می‌شود؛ نبودش یعنی آخرین id، و صفحهٔ تکراری سوییپ را تمام می‌کند (نه حلقهٔ بی‌پایان) |
| `get_full_group` برای گروهی که نمی‌بینی `None` برمی‌گرداند، نه خطا | `AttributeError` یک خط بعد، بدون اینکه بدانی چرا | `get_group` با `GroupUnavailableError` صریح می‌ترکد |
| `Permissions()` هر بیست پرچمش پیش‌فرض `False` است و `set_member_permissions` همان را عیناً می‌فرستد | «فقط ارسال مدیا را ببند» در عمل **همهٔ اختیارات عضو را می‌گیرد** | `restrict`/`allow` اول دسترسی فعلی را می‌خوانند و فقط پرچم نام‌برده را عوض می‌کنند |
| `GetPinsResponse` روی هر پیام پین‌شده `ChatType.GROUP` می‌چسباند | پین‌های کانال/سوپرگروه با نوع peer اشتباه برمی‌گردند و ریپلای/ری‌اکشن رویشان کار نمی‌کند | `pins()` نوع واقعی چت را می‌گیرد و برمی‌گرداند |
| `LoadHistory` offset را تایم‌استمپ می‌گیرد نه message id، و کتابخانه صفحه‌بندی ندارد | پیام‌های هم‌میلی‌ثانیه بریده می‌شوند یا (با offsetِ inclusive) پیمایش بعد از یک صفحه می‌ایستد | `iter_history` اول صفحه را بزرگ می‌کند، بعد offset را یک میلی‌ثانیه عقب می‌برد |

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
| استیکر | ✅ `STICKER` (+id، مجموعه، دو رندیشن قابل دانلود) | ✅ `send_sticker` (از پیام دریافتی) | ✅ |
| لوکیشن | ✅ `LOCATION` (+lat/lon) | ✅ `send_location` | — |
| کارت مخاطب | ✅ `CONTACT` (+نام، شماره‌ها، ایمیل‌ها) | ✅ `send_contact` | — |

`BaleClient 1.0.9` برای این سه نوع آخر هیچ مدلی ندارد: روی سیم در فیلدهای
protobufی می‌آیند که کتابخانه اعلامشان نکرده (فیلد `7` یک پیام JSON با
`dataType` است — لوکیشن و مخاطب — و فیلد `12` استیکر). دو چیز بدون فورک
کارشان را ممکن می‌کند: `BaleObject` فیلدهای ناشناخته را در `model_extra` نگه
می‌دارد و انکودر protobuf هم schema-less است. شماره‌فیلدها با
`tools/probe_content.py` از ترافیک واقعی کشف شده‌اند؛ `describe()` آن‌ها را
مثل هر نوع دیگری می‌فهمد و `bale_userbot.extras` سمت ارسالشان است.

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
python -m bale_userbot login                # شماره → کد پیامکی → data/session.bale
python -m bale_userbot whoami               # نشست ذخیره‌شده مال کدام اکانت است
python -m bale_userbot whoami --offline     # بدون اتصال؛ فقط آیدی
```

از این به بعد نشست از فایل خوانده می‌شود و OTP لازم نیست.

## خط فرمان (بدون نوشتن کد)

```bash
python -m bale_userbot groups                          # گروه/کانال‌های این اکانت
python -m bale_userbot members 12345                   # اعضا + نام و یوزرنیم
python -m bale_userbot members 12345 --admins          # فقط ادمین‌ها
python -m bale_userbot members 12345 --json > m.json   # ردیف‌های خام
python -m bale_userbot history 12345 --since 2026-04-26 --until 2026-04-28
python -m bale_userbot history 12345 --chat-type channel --limit 500 --json
python -m bale_userbot pins 12345
python -m bale_userbot link 12345 [--revoke]
```

`--json` همان ردیف‌هایی را می‌دهد که `export_members`/`export_history`
برمی‌گردانند. لاگ‌های این دستورها روی stderr می‌روند تا خروجی JSON روی stdout
قابل pipe کردن بماند.

---

## API

**`BaleApp`** — اجرا و ثبت هندلر:

```python
app = BaleApp(Config.from_env())

@app.on_message(kinds=[MessageKind.TEXT])          # فیلتر بر اساس نوع
async def on_text(message, client): ...

@app.on_message(IsMedia(), FromUsers(123, 456))     # فیلترهای دلخواه
async def on_media(message, client): ...

app.run()                            # مسدودکننده
await app.start()                    # داخل event loop خودت
await app.start(background=True)     # فقط وصل شو و برگرد — برای اسکریپت یک‌باره
```

گیت‌های سطح config (خصوصی/گروه، allowlist، پیام‌های خودت) قبل از فیلترهای تو
اعمال می‌شوند.

**`describe(message) -> MessageInfo`** — نمای صاف پیام:
`kind`, `text`, `caption`, `body`, `media`, `has_keyboard`, `is_forward`,
`reply_to_id`, `sender_id`, `chat_id`, `chat_type`, `service_text`,
`location` (`LocationInfo`), `contact` (`ContactInfo`), `sticker`
(`StickerInfo`), `json_payload` (بدنهٔ خامِ هر پیام JSON — برای dataTypeهایی
که bale-userbot هنوز نمی‌شناسد؛ `kind` آن‌ها `UNKNOWN` می‌ماند ولی payload در
دسترس است).
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

await app.send_location(35.6892, 51.3890, chat_id)
await app.send_contact("Ali", ["0912…"], chat_id)
await app.send_sticker(received_sticker_message, chat_id)   # یا info.sticker

# همان‌ها در پاسخ به یک پیام — chat_id و chat_type از خود پیام گرفته می‌شوند
await app.reply_location(message, 35.6892, 51.3890)
await app.reply_contact(message, "Ali", ["0912…"])
await app.reply_sticker(message, received_sticker_message)
await app.reply_content(message, any_message_content)
```

`app.download(message)` فایل را در پوشهٔ دانلود می‌نویسد و `Path` می‌دهد؛
`destination=None` صریح، خودِ بایت‌ها را برمی‌گرداند. اسم فایل پیش‌فرض با نوع
پیام ساخته می‌شود (`sticker-<id>.png`, `voice-<id>.ogg`).

**ری‌اکشن و «در حال تایپ»:**

```python
await app.react(message, "👍")
await app.unreact(message, "👍")
await app.typing(chat_id, mode=TypingMode.SENDINGPHOTO)
await app.typing(chat_id, stop=True)
```

برخلاف متدهای خود `BaleClient`، این‌ها در ربات/سوپرگروه/کانال هم کار می‌کنند
(جدول بالا).

استیکر یعنی ارجاع به یک مجموعهٔ سمت سرور؛ ساختن استیکر جدید از این‌جا ممکن
نیست — همانی را می‌فرستی که قبلاً دریافت (یا در history دیده‌ای).

`resend` هم لوکیشن و مخاطب و استیکر را عیناً کپی می‌کند (بدون بازسازی از
`LocationInfo`/`ContactInfo`، تا هیچ زیرفیلدِ مدل‌نشده‌ای گم نشود).

بیلدرهای سطح پایین هم export شده‌اند: `location_content` / `contact_content`
/ `sticker_content` / `json_content` / `json_block_content` یک
`MessageContent` آماده می‌سازند و `send_content` هر `MessageContent`
دلخواهی را می‌فرستد — برای وقتی که بله نوع تازه‌ای اضافه کرد و نخواستی
منتظر bale-userbot بمانی.

**گروه‌ها و اعضا:**

```python
count  = await app.member_count(chat_id)          # فقط تعداد — یک درخواست
group  = await app.group(chat_id)                 # GroupInfo: عنوان، مالک، نوع، تعداد
members = await app.members(chat_id)              # همهٔ اعضا (id و نقش)
members = await app.members(chat_id, profiles=True)   # + نام و یوزرنیم
admins  = await app.admins(chat_id)               # ادمین‌ها و مالک (فیلتر سمت سرور)
groups  = await app.groups()                      # همهٔ گروه/کانال‌های این اکانت

async for m in app.iter_members(chat_id):         # گروه بزرگ، بدون بارِ حافظه
    ...

members = await app.hydrate_profiles(old_snapshot)  # اسنپ‌شات ذخیره‌شده را نام‌دار کن
```

`GroupInfo`: `id`, `title`, `members_count`, `chat_type`, `access_hash`,
`username`, `about`, `owner_id`, `created_at`, `is_joined`,
`available_reactions`, `default_permissions`, `known_members` +
`is_channel` / `is_public`.

`MemberInfo`: `user_id`, `chat_id`, `is_admin`, `is_owner`, `inviter_id`,
`joined_at`, `promoted_by`, `promoted_at` و — پس از hydrate — `name`,
`local_name`, `username`, `access_hash`, `is_bot`, `is_deleted`,
`account_created_at`, `profile_loaded`.

`profile_loaded` عمداً وجود دارد: بدون آن نمی‌شود فهمید «این عضو یوزرنیم
ندارد» یا «اصلاً نپرسیده‌ایم».

`name` تنها استثناست: سرور در لیست اعضای خودِ گروه گاهی نام نمایشی را
می‌فرستد (در فیلدی که `Member.date` اعلام شده)، و پچ آن را نگه می‌دارد. پس
ممکن است `name` پر باشد ولی `profile_loaded` هنوز `False` — یعنی اسم داریم و
بقیهٔ پروفایل نه. اگر بعداً `profiles=True` بزنی، پروفایلِ واقعی جایش را
می‌گیرد. برای اینکه ببینی روی اکانت تو چقدر رایگان درمی‌آید:
`python -m bale_userbot members <chat_id> --no-profiles`.

`members_count` همیشه عدد کاملِ گروه است — حتی در کانالی که پیمایش صفحه‌به‌صفحه
هرگز به همهٔ اعضایش نمی‌رسد. `chat_type` هم فقط از `ex_info` درمی‌آید: در پاسخ
سرور، سوپرگروه و گروه ساده `group_type` یکسانی دارند و هر `send_*` به تفکیک
درست نیاز دارد.

**ذخیره‌سازی — مرز کار ما:** `export_members` داده می‌دهد، نه فایل.

```python
group, rows = await app.export_members(chat_id)   # rows: list[dict] تخت و JSON-پذیر
json.dump(rows, open("members.json", "w"))        # کار برنامهٔ تو
db.executemany("INSERT …", rows)                  #     ← نه کار زیرساخت
```

هر ردیف `member.as_dict()` است به‌علاوهٔ `group_id`/`group_title`/
`group_username`. `member_records(members, group)` هم مستقیم در دسترس است.
نمونهٔ کامل (JSON + SQLite): `examples/export_members.py`.

**رصد تغییرات عضویت:**

```python
changes, current = await app.watch_membership(chat_id, previous_snapshot)
if changes:                              # False وقتی هیچ چیز عوض نشده
    changes.joined, changes.left, changes.promoted, changes.demoted, changes.renamed
```

`diff_members(before, after)` تابع خالص است — هیچ درخواستی نمی‌زند — پس
می‌توانی اسنپ‌شات دیشب را از هرجا که ذخیره کرده‌ای بخوانی و مقایسه کنی.
`watch_membership` علاوه بر تغییرات، اسنپ‌شات جدید را هم برمی‌گرداند، چون
باید ذخیره‌اش کنی تا دفعهٔ بعد مبنای مقایسه باشد. تغییر نام فقط وقتی گزارش
می‌شود که هر دو اسنپ‌شات `profiles=True` داشته باشند، وگرنه همهٔ اعضا
«تغییرنام‌داده» به نظر می‌رسند. نمونه: `examples/watch_membership.py`.

---

### آرشیو پیام‌ها (با بازهٔ زمانی)

```python
from datetime import date, datetime

msgs = await app.history(chat_id, ChatType.GROUP,
                         since=date(2026, 4, 26), until=date(2026, 4, 28))
msgs = await app.history(chat_id, ChatType.GROUP,
                         since="2026-04-26", until="2026-04-28T18:00")
rows = await app.export_history(chat_id, ChatType.GROUP, since="2026-04-26")

async for m in app.iter_history(chat_id, ChatType.GROUP, limit=1000):
    ...                                   # از جدید به قدیم، بدون بارِ حافظه
```

`since`/`until` هر چیزی را قبول می‌کنند: `date`، `datetime`، رشتهٔ ISO، یا
تایم‌استمپ میلی‌ثانیه‌ای خود بله. **تاریخ خالی یعنی تمام آن روز** — پس
`until=date(2026, 4, 28)` تا آخرین میلی‌ثانیهٔ ۲۸ام را می‌گیرد، نه تا نیمه‌شبِ
اولش. `datetime` بدون timezone به وقت **محلی** خوانده می‌شود، چون کسی که
«۲۶ ساعت ۹ صبح» می‌نویسد ساعت ۹ همان‌جا را می‌خواهد نه UTC.

`app.history()` به‌ترتیب زمانی (قدیم→جدید) برمی‌گرداند؛ `oldest_first=False`
عکسش. `export_history` همان ردیف‌های تخت `describe()` را می‌دهد.

صفحه‌بندی `LoadHistory` فقط یک تایم‌استمپ به‌عنوان offset دارد، نه message id.
دو تلهٔ ناشی از همین، این‌جا هندل شده‌اند و هر دو از بیرون یک شکل دارند —
صفحه‌ای که هیچ پیام جدیدی ندارد: یا سرور offset را **شامل** حساب کرده (یک
میلی‌ثانیه عقب می‌رویم)، یا پیام‌های هم‌میلی‌ثانیه بیشتر از ظرفیت صفحه‌اند
(صفحه را بزرگ‌تر می‌خواهیم). اول بزرگ‌کردن، بعد عقب‌رفتن — وگرنه یک رگبار پیام
که در یک میلی‌ثانیه فرستاده شده بی‌صدا بریده می‌شود.

---

### مدیریت گروه

```python
await app.kick(chat_id, user_id)
await app.unban(chat_id, user_id)
ids = await app.banned(chat_id)

await app.promote(chat_id, user_id, title="ناظر", delete_message=True)
await app.demote(chat_id, user_id)

await app.restrict(chat_id, user_id, "send_media")      # فقط همین یکی خاموش
await app.allow(chat_id, user_id, "pin_message")        # فقط همین یکی روشن
await app.mute(chat_id, user_id)                        # همهٔ راه‌های حرف‌زدن
await app.unmute(chat_id, user_id)
perms = await app.permissions_of(chat_id, user_id)

await app.set_permissions(chat_id, user_id, send_message=False, pin_message=True)
await app.set_default_permissions(chat_id, send_media=False)   # پیش‌فرضِ کل گروه
```

**چرا این لایه لازم است:** `Permissions()` هر بیست پرچمش پیش‌فرض `False` است.
یعنی `set_member_permissions(chat, user, Permissions(send_message=False))` کسی
را ساکت نمی‌کند — **همهٔ اختیاراتش را می‌گیرد**. `restrict`/`allow` اول
دسترسی فعلی را می‌خوانند و فقط پرچم‌هایی را که اسم برده‌ای عوض می‌کنند. اسم
اشتباه هم *قبل از* هر درخواستی `ValueError` می‌دهد. فهرست کامل در
`PERMISSION_FLAGS`.

بله متد جداگانه‌ای برای «بن» ندارد: حذف عضو همان `kick()` است و سرور فهرستی
نگه می‌دارد که با `banned()` می‌خوانی و با `unban()` پاک می‌کنی.

`promote` عمداً دو کار را یکجا می‌کند: در بله ادمین‌کردن هیچ اختیاری نمی‌دهد و
باید بلافاصله پرمیشن ست شود.

**پین و لینک دعوت:**

```python
pinned = await app.pins(chat_id)          # با chat_type درست (نه GROUP همیشگی)
await app.pin(chat_id, message)
await app.unpin(chat_id, message)
await app.unpin_all(chat_id)

link = await app.invite_link(chat_id)              # link.url
link = await app.invite_link(chat_id, revoke=True) # لینک قبلی می‌سوزد
info = await app.preview("https://ble.ir/join/…")  # قبل از عضو شدن
await app.join("https://ble.ir/join/…")
await app.leave(chat_id)
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
- `examples/export_members.py` — استخراج اعضای یک گروه در JSON و SQLite
- `examples/watch_membership.py` — گزارش آمد و رفت اعضا بین دو اجرا

---

## تست

```bash
python -m pytest tests -q      # 264 تست، بدون شبکه و بدون اکانت
```

تست‌ها با اشیای واقعی `baleclient.types` ساخته می‌شوند (نه mock پروتکل): هر شکل
محتوا یک‌بار از مسیر `Dispatcher → filter → handler` عبور داده می‌شود، و
`tests/test_wire.py` پیام‌های کامل را از روی دیکشنری‌های عددیِ همان چیزی که
روی سیم می‌آید پارس می‌کند. چند تست عمداً باگ‌های بالادست (و رفعشان در
`patches.py`) را assert می‌کنند؛ اگر روزی fail شدند یعنی `BaleClient`
درستشان کرده و می‌شود workaround را حذف کرد.

**تست زنده** (نیازمند اکانت واقعی — در CI اجرا نمی‌شود):

```bash
python tools/live_check.py            # به «پیام‌های ذخیره‌شده»ی خودت
python tools/live_check.py --chat ID --keep
```

هر نوع پیام را می‌فرستد (شامل لوکیشن، مخاطب و استیکر)، از history می‌خواند،
دوباره classify می‌کند، یکی را دانلود می‌کند و در پایان پیام‌های خودش را پاک
می‌کند.

`tools/probe_content.py` هم فقط‌خواندنی است: هر پیامی را که `describe()`
نمی‌شناسد یا فیلد اضافه دارد با شماره‌فیلدهای خام چاپ می‌کند — همان ابزاری که
لوکیشن/مخاطب/استیکر با آن کشف شدند.

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
- ابعاد ویدئو و مدت زمان به‌صورت خودکار استخراج نمی‌شوند (نیازمند ffprobe است)؛
  اگر می‌خواهی پیش‌نمایش درست باشد خودت `width`/`height`/`duration` را بده.
- `MessageKind.FORWARD` یعنی «استاب خالی»؛ محتوای اصلی فوروارد در پیام نقل‌شده
  (`message.replied_to`) است.
- `patches.py` هنگام import، والیدیتورهای `MessageContent` را عوض می‌کند. این
  تغییر سراسری و در سطح فرآیند است: اگر در همان پروسه کد دیگری مستقیم روی
  `BaleClient` کار می‌کند، رفتار درست‌شده را می‌بیند (نه رفتار باگ‌دار را).
