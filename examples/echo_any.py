"""Echo back whatever arrives — text, photo, video, voice, music, gif, file.

Media is bounced back by file id, so nothing is downloaded or re-uploaded.
"""

from balekit import BaleApp, Config, MessageKind

app = BaleApp(Config.from_env())


@app.on_message()
async def echo(message, client):
    info = app.describe(message)

    if info.kind is MessageKind.TEXT:
        await message.answer(info.text)
    elif info.is_media:
        await app.resend(message)
    elif info.kind is MessageKind.GIFT:
        await message.answer("gift received")
    else:
        await message.answer(f"nothing to echo (kind={info.kind.value})")


app.run()
