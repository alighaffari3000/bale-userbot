"""Minimal smoke test against a real account — echoes private text messages.

Note the imports: BaleClient 1.0.9 still ships upstream examples that import
`aiobale` (the project it was forked from). The installed package is
`baleclient`; `import aiobale` fails.
"""

from baleclient import Client, Dispatcher
from baleclient.filters import IsPrivate, IsText
from baleclient.types import Message

dp = Dispatcher()
client = Client(dp, session_file="./data/session.bale")


@dp.message(IsPrivate(), IsText())
async def echo(msg: Message, client: Client) -> None:
    if msg.sender_id == client.id:  # never answer ourselves
        return
    await msg.answer(msg.text)


client.run()
