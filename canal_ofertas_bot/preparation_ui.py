"""Keep the preparation prompt and its URL warnings in one private flow."""
from .amazon import URL_WARNING


class PreparationScreens:
    async def clear_preparation(self, message_id=None, *, keep_screen=False):
        session = self.store.preparation(self.owner)
        if session is None or (message_id is not None and session['message_id'] != message_id):
            return
        for warning in session['aux']:
            await self.remove(warning)
        if not keep_screen and session['message_id'] is not None:
            await self.remove(session['message_id'])
        self.store.clear_preparation(self.owner)

    async def warn_invalid_link(self, message):
        # The rejected input is removed, while the preparation screen stays usable.
        await self.remove(message['message_id'])
        session = self.store.preparation(self.owner)
        if session:
            for warning in session['aux']:
                await self.remove(warning)
        sent = await self.say(URL_WARNING)
        self.store.add_preparation_warning(self.owner, sent['message_id'])
        self.store.track_message(self.owner, sent['message_id'], self.store.now() + self.store.ttl)
