"""Save every attachment that arrives into the configured download directory."""

from bale_userbot import BaleApp, Config, IsMedia

app = BaleApp(Config.from_env())


@app.on_message(IsMedia())
async def save(message, client):
    path = await app.download(message)
    await message.reply(f"saved as {path.name}")


app.run()
