"""Print a one-line description of every incoming message.

The quickest way to see what `describe()` makes of real traffic.
"""

from bale_userbot import BaleApp, Config

app = BaleApp(Config.from_env())


@app.on_message()
async def show(message, client):
    info = app.describe(message)
    media = info.media
    detail = ""
    if info.location:
        detail = f"at=({info.location.latitude}, {info.location.longitude})"
    elif info.contact:
        detail = f"contact={info.contact.name} {','.join(info.contact.phones)}"
    elif media:
        detail = (
            f"file={media.name or '-'} {media.mime_type} {media.size}B "
            f"{media.width}x{media.height} dur={media.duration}"
        )
    print(
        f"[{info.kind.value:<8}] chat={info.chat_id} from={info.sender_id} "
        f"body={(info.body or '')[:40]!r} {detail}"
    )


app.run()
