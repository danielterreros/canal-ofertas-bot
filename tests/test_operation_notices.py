"""Notices are retired only by a related successful action or explicit acknowledgement."""
from datetime import datetime
import unittest
from unittest.mock import AsyncMock, patch

import test_menus_scheduled as fixtures
import test_chat_cleanup as cleanup
import test_approved_changes as workflows
from canal_ofertas_bot.domain import Product
from canal_ofertas_bot.config import Config
from canal_ofertas_bot.service import Service
from canal_ofertas_bot.storage import Store
from canal_ofertas_bot.telegram import TelegramError
from canal_ofertas_bot.network import NetworkError
from canal_ofertas_bot.notices import current_product_warnings

OWNER, OTHER, CHANNEL, URL = fixtures.OWNER, fixtures.OTHER, fixtures.CHANNEL, fixtures.URL


class NoticeFlows(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ScheduledTests.setUp
    sent = fixtures.ScheduledTests.sent
    new_preview = fixtures.ScheduledTests.new_preview
    new_post = fixtures.ScheduledTests.new_post
    command = cleanup.CleanupTests.command
    click = fixtures.ScheduledTests.click
    q = fixtures.ScheduledTests.q
    text = fixtures.ScheduledTests.text
    latest_view = fixtures.ScheduledTests.latest_view
    view = fixtures.ScheduledTests.view
    deleted = cleanup.CleanupTests.deleted
    reset_api = cleanup.CleanupTests.reset_api
    generated = workflows.Workflows.generated
    edit_new = workflows.Workflows.edit_new
    incoming_photo = workflows.Workflows.incoming_photo
    photo = workflows.Workflows.photo

    def notices(self, owner=OWNER):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM operation_notices WHERE owner=? AND message_id IS NOT NULL ORDER BY rowid', (owner,))]

    def notice(self, issue=None, owner=OWNER):
        rows = self.notices(owner)
        return next(r for r in reversed(rows) if issue is None or r['issue'] == issue)

    def restart(self):
        self.store = Store(self.store.path, ttl=self.store.ttl, now=lambda: self.now)
        self.bot = Service(self.config, self.store, self.api)

    async def test_correct_price_retires_error_and_failed_command_but_title_does_not(self):
        self.new_preview()
        await self.command('/editar precio abc', 900)
        notice = self.notice('field:precio')
        self.assertNotIn(900, self.deleted())
        await self.command('/start', 901)
        await self.command('/editar titulo Nuevo título', 902)
        self.assertNotIn(notice['message_id'], self.deleted())
        self.assertNotIn(900, self.deleted())
        await self.command('/editar precio 150,00', 903)
        self.assertIn(notice['message_id'], self.deleted())
        self.assertIn(900, self.deleted())
        self.assertIn(903, self.deleted())
        self.assertFalse(self.notices())

    async def test_repeated_problem_replaces_only_previous_error_and_input(self):
        self.new_preview()
        await self.command('/editar precio abc', 900)
        first = self.notice()
        await self.command('/editar anterior abc', 901)
        other_field = self.notice()
        self.reset_api()
        await self.command('/editar precio xyz', 902)
        self.assertIn(first['message_id'], self.deleted())
        self.assertIn(900, self.deleted())
        self.assertNotIn(other_field['message_id'], self.deleted())
        self.assertNotIn(901, self.deleted())
        self.assertNotIn(902, self.deleted())
        self.assertEqual(len(self.notices()), 2)

    async def test_bare_instructions_do_not_resolve_invalid_field(self):
        self.new_preview()
        await self.command('/editar precio abc', 900)
        error = self.notice()
        self.reset_api()
        await self.command('/editar', 901)
        self.assertNotIn(error['message_id'], self.deleted())
        self.assertNotIn(900, self.deleted())
        self.assertIn(901, self.deleted())

    async def test_error_is_private_to_its_user(self):
        self.new_preview(OWNER)
        self.new_preview(OTHER)
        await self.command('/editar precio abc', 900, actor=OTHER)
        other = self.notice(owner=OTHER)
        await self.command('/editar precio 150,00', 901)
        self.assertNotIn(other['message_id'], self.deleted(OTHER))
        self.assertEqual(len(self.notices(OTHER)), 1)

    async def test_correct_caption_does_not_remove_rejected_image_notice(self):
        initial = await self.generated()
        await self.edit_new(initial)
        self.api.download_photo = AsyncMock(return_value=b'invalid-image')
        self.serial += 1
        bad_input = self.serial
        await self.bot.handle({'message': {'message_id': bad_input, 'photo': [{'file_id': 'bad'}],
                              'from': {'id': OWNER}, 'chat': {'id': OWNER, 'type': 'private'}}})
        error = self.notice('image')
        await self.text(initial['caption'])
        self.assertNotIn(error['message_id'], self.deleted())
        self.assertNotIn(bad_input, self.deleted())
        current = self.store.preview(OWNER)
        await self.edit_new(current)
        await self.incoming_photo()
        self.assertIn(error['message_id'], self.deleted())
        self.assertIn(bad_input, self.deleted())

    async def test_closing_editor_discards_its_temporary_errors(self):
        first = self.new_preview()
        await self.edit_new(first)
        await self.text('x' * 1100)
        error = self.notice('caption')
        editor = self.store.editor(OWNER)
        await self.click(f"e:{editor['preview_id']}:close", editor['prompt'])
        self.assertIn(error['message_id'], self.deleted())
        self.assertFalse(self.notices())
        self.assertEqual(self.store.preview(OWNER)['caption'], first['caption'])

    async def test_schedule_error_survives_start_and_disappears_on_confirmation(self):
        self.new_preview()
        await self.command('/programar incorrecto', 900)
        error = self.notice('schedule-date')
        await self.command('/start', 901)
        self.assertNotIn(error['message_id'], self.deleted())
        due = datetime.fromtimestamp(self.now + 3600, self.config.timezone).strftime('%d/%m/%Y %H:%M')
        await self.command('/programar ' + due, 902)
        self.assertIn(error['message_id'], self.deleted())
        self.assertIn(900, self.deleted())
        self.assertEqual(len(self.store.pending_all((OWNER,))), 1)

    async def test_disabled_publication_notice_requires_successful_programming(self):
        self.new_preview()
        self.bot.config = Config.from_env({**self.config.values, 'ENABLE_PUBLISH': 'false'})
        due = datetime.fromtimestamp(self.now + 3600, self.config.timezone).strftime('%d/%m/%Y %H:%M')
        await self.command('/programar ' + due, 900)
        notice = self.notice('publishing-disabled')
        self.assertEqual(notice['kind'], 'verify')
        await self.command('/start', 901)
        self.assertNotIn(notice['message_id'], self.deleted())
        self.bot.config = self.config
        await self.command('/programar ' + due, 902)
        self.assertIn(notice['message_id'], self.deleted())

    async def test_generation_configuration_notice_requires_verified_render(self):
        self.store.replace_draft(OWNER, self.product, 1)
        self.store.update_draft(OWNER, self.product, self.photo())
        with patch('canal_ofertas_bot.service.render_card', side_effect=ValueError('Faltan las fuentes DejaVu dentro de la imagen; revisa FONT_DIR.')):
            await self.command('/previa', 900)
        notice = self.notice('render-configuration')
        await self.command('/start', 901)
        self.assertNotIn(notice['message_id'], self.deleted())
        await self.command('/previa', 902)
        self.assertIn(notice['message_id'], self.deleted())
        self.assertIn(900, self.deleted())

    async def test_valid_new_draft_resolves_missing_draft_and_old_temporary_errors(self):
        await self.command('/datos', 900)
        missing = self.notice('missing-draft')
        self.bot.amazon.extract = AsyncMock(return_value=Product(URL, 'Nuevo producto'))
        await self.text(URL)
        self.assertIn(missing['message_id'], self.deleted())
        self.assertIn(900, self.deleted())
        await self.command('/editar precio abc', 901)
        error = self.notice('field:precio')
        await self.text(URL)
        self.assertIn(error['message_id'], self.deleted())
        self.assertIn(901, self.deleted())

    async def test_complete_preview_resolves_missing_fields_only_when_ready(self):
        self.store.replace_draft(OWNER, Product(URL), 1)
        await self.command('/previa', 900)
        error = self.notice('incomplete-draft')
        await self.command('/editar titulo Producto', 901)
        self.assertNotIn(error['message_id'], self.deleted())
        await self.command('/editar precio 29,99', 902)
        await self.incoming_photo()
        self.assertNotIn(error['message_id'], self.deleted())
        await self.command('/previa', 903)
        self.assertIn(error['message_id'], self.deleted())
        self.assertIn(900, self.deleted())

    async def test_unknown_command_is_replaced_and_resolved_by_recognized_command(self):
        await self.command('/noexiste', 900)
        first = self.notice()
        await self.command('/tampoco', 901)
        second = self.notice()
        self.assertIn(first['message_id'], self.deleted())
        self.assertIn(900, self.deleted())
        self.assertNotIn(901, self.deleted())
        await self.command('/id', 902)
        self.assertIn(second['message_id'], self.deleted())
        self.assertIn(901, self.deleted())

    async def test_nonexistent_post_notice_resolves_after_pending_consultation(self):
        await self.command('/anular noexiste', 900)
        notice = self.notice('unknown-post')
        await self.command('/programadas', 901)
        self.assertIn(notice['message_id'], self.deleted())
        self.assertIn(900, self.deleted())

    async def test_discard_retires_errors_without_affecting_scheduled_post(self):
        post = self.new_post()
        self.new_preview()
        await self.command('/editar precio abc', 900)
        error = self.notice()
        await self.command('/cancelar', 901)
        self.assertIn(error['message_id'], self.deleted())
        self.assertIn(900, self.deleted())
        self.assertEqual(self.store.get_post(post['id']), post)

    async def test_failed_notice_replacement_keeps_previous_until_related_success(self):
        self.new_preview()
        await self.command('/editar precio abc', 900)
        first = self.notice()
        self.api.message.side_effect = TelegramError(503)
        await self.command('/editar precio xyz', 901)
        self.assertNotIn(first['message_id'], self.deleted())
        self.assertNotIn(900, self.deleted())
        self.api.message.side_effect = self.sent
        await self.command('/editar precio 150,00', 902)
        self.assertIn(first['message_id'], self.deleted())
        self.assertIn(900, self.deleted())
        self.assertIn(901, self.deleted())

    async def test_resolved_notice_deletions_retry_after_restart_and_backoff(self):
        self.new_preview()
        await self.command('/editar precio abc', 900)
        error = self.notice()
        def fail_delete(method, **kwargs):
            if method == 'deleteMessage' and kwargs['message_id'] == error['message_id']:
                raise TelegramError(429, retry_after=60)
            return True
        self.api.call.side_effect = fail_delete
        await self.command('/editar precio 150,00', 901)
        self.assertFalse(self.notices())
        self.assertIn(900, self.deleted())
        self.restart()
        self.api.call.side_effect = None
        self.reset_api()
        await self.bot.notices.cleanup((OWNER,))
        self.assertNotIn(error['message_id'], self.deleted())
        self.now += 61
        await self.bot.notices.cleanup((OWNER,))
        self.assertIn(error['message_id'], self.deleted())
        self.assertFalse(self.store.notice_deletions((OWNER,)))

    async def test_connection_error_is_resolved_only_by_same_field_operation(self):
        self.new_preview()
        def fail_saved(chat, text, *args, **kwargs):
            if text.startswith('Dato guardado'): raise TelegramError(503)
            return self.sent(chat, text, *args, **kwargs)
        self.api.message.side_effect = fail_saved
        await self.command('/editar precio 150,00', 900)
        notice = self.notice('operation:/editar:precio')
        self.assertEqual(notice['kind'], 'verify')
        self.api.message.side_effect = self.sent
        await self.command('/editar titulo Producto', 901)
        self.assertNotIn(notice['message_id'], self.deleted())
        await self.command('/editar precio 150,00', 902)
        self.assertIn(notice['message_id'], self.deleted())
        self.assertIn(900, self.deleted())

    async def test_post_caption_error_is_not_removed_by_another_drafts_price(self):
        post = self.new_post()
        self.new_preview()
        await self.q(self.view(post, 'preview', {'media': True}), 'edit')
        await self.text('x' * 1100)
        notice = self.notice('caption')
        self.assertEqual(notice['scope'], 'post:' + post['id'])
        await self.command('/editar precio 150,00', 900)
        self.assertNotIn(notice['message_id'], self.deleted())
        await self.text(post['caption'])
        self.assertIn(notice['message_id'], self.deleted())
        self.assertEqual(self.store.get_post(post['id']), post)

    async def test_cancellation_update_error_is_removed_when_card_is_updated(self):
        post = self.new_post()
        def fail_caption(method, **kwargs):
            if method == 'editMessageCaption': raise TelegramError(503)
            return True
        self.api.call.side_effect = fail_caption
        await self.command('/anular ' + post['id'], 900)
        notice = self.notice('cancel-update')
        self.api.call.side_effect = None
        await self.bot.delivery.notify()
        self.assertIn(notice['message_id'], self.deleted())
        self.assertIn(900, self.deleted())
        self.assertEqual(self.store.get_post(post['id'])['status'], 'cancelled')

    async def test_cancel_update_error_gets_confirmation_if_card_disappears(self):
        post = self.new_post()
        def fail_caption(method, **kwargs):
            if method == 'editMessageCaption': raise TelegramError(503)
            return True
        self.api.call.side_effect = fail_caption
        await self.command('/anular ' + post['id'], 900)
        notice = self.notice('cancel-update')
        self.api.call.side_effect = TelegramError(400)
        await self.bot.delivery.notify()
        self.assertIn('🚫 Cancelada', self.api.message.await_args.args[1])
        self.assertFalse(self.notices())

    async def failed_publication(self, uncertain=False):
        post = self.new_post()
        self.now = post['due'] + 1
        self.api.photo.side_effect = NetworkError('incomplete', uncertain=True) if uncertain else TelegramError(403)
        await self.bot.delivery.tick()
        return post, self.notice('publication-result')

    async def test_critical_notice_persists_through_expiry_restart_and_unrelated_actions(self):
        post, notice = await self.failed_publication()
        self.assertEqual(notice['kind'], 'critical')
        self.assertEqual(self.api.message.await_args.args[2][0][0]['text'], '✅ - Revisado')
        self.now += self.store.ttl + 86400
        self.restart()
        await self.command('/start', 900)
        await self.command('/programadas', 901)
        await self.bot.delivery.tick()
        self.assertNotIn(notice['message_id'], self.deleted())
        self.assertEqual(self.notice()['token'], notice['token'])

    async def test_review_of_uncertain_send_deletes_only_notice_and_never_resends(self):
        post, notice = await self.failed_publication(uncertain=True)
        with self.store.connect() as db:
            before = dict(db.execute('SELECT * FROM receipts WHERE id=?', (post['id'],)).fetchone())
        self.reset_api()
        await self.click(f"n:{notice['token']}:review", notice['message_id'])
        self.assertEqual(self.deleted(), {notice['message_id']})
        self.api.photo.assert_not_awaited()
        self.assertFalse(self.notices())
        with self.store.connect() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM receipts WHERE id=?', (post['id'],)).fetchone()), before)
        await self.click(f"n:{notice['token']}:review", notice['message_id'])
        self.api.photo.assert_not_awaited()

    async def test_review_rejects_other_owner_and_wrong_message(self):
        post, notice = await self.failed_publication()
        self.reset_api()
        await self.click(f"n:{notice['token']}:review", notice['message_id'], actor=OTHER)
        await self.click(f"n:{notice['token']}:review", notice['message_id'] + 1)
        self.assertEqual(len(self.notices()), 1)
        self.assertFalse(self.deleted())
        self.assertFalse(self.deleted(OTHER))
        self.api.photo.assert_not_awaited()

    async def test_critical_delivery_notification_retries_without_resending_publication(self):
        post = self.new_post()
        self.now = post['due'] + 1
        self.api.photo.side_effect = TelegramError(403)
        self.api.message.side_effect = TelegramError(503)
        await self.bot.delivery.tick()
        self.assertEqual(len(self.store.notices(OWNER)), 1)
        self.api.message.side_effect = self.sent
        await self.bot.delivery.tick()
        self.assertFalse(self.store.notices(OWNER))
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.api.photo.await_count, 1)
        self.api.message.reset_mock()
        await self.bot.delivery.notify()
        self.api.message.assert_not_awaited()

    async def test_sending_or_saving_connection_error_requires_explicit_review(self):
        preview = self.new_preview()
        self.api.call.side_effect = NetworkError('failed', uncertain=True)
        await self.click(f"p:{preview['id']}:publish", preview['message_id'], photo=True)
        notice = self.notice('operation:p:publish')
        self.assertEqual(notice['kind'], 'critical')
        self.api.call.side_effect = None
        await self.command('/start', 900)
        self.assertNotIn(notice['message_id'], self.deleted())

    async def test_popup_validation_does_not_add_persistent_notice(self):
        preview = self.new_preview()
        await self.click('p:obsolete:publish', preview['message_id'], photo=True)
        self.api.message.assert_not_awaited()
        self.assertFalse(self.notices())

    async def test_correctable_field_errors_use_the_same_cleanup_rule(self):
        cases = [('/editar titulo ' + 'x'*111, '/editar titulo Producto'),
                 ('/editar emoji ' + 'x'*13, '/editar emoji 📦'),
                 ('/editar condiciones ' + 'x'*201, '/editar condiciones Requiere cupón'),
                 ('/editar campaña inexistente', '/editar campana Black Friday'),
                 ('/editar campo valor', '/editar precio 150,00')]
        for bad, good in cases:
            with self.subTest(command=bad):
                self.new_preview()
                await self.command(bad, 900)
                error = self.notice()
                self.reset_api()
                await self.command(good, 901)
                self.assertIn(error['message_id'], self.deleted())
                self.assertIn(900, self.deleted())

    async def test_price_reference_error_is_not_resolved_by_changing_title(self):
        self.new_preview()
        self.store.update_draft(OWNER, self.product, self.photo())
        await self.command('/editar anterior 100,00', 900)
        await self.command('/previa', 901)
        error = self.notice('reference-order')
        await self.command('/editar titulo Otro título', 902)
        self.assertNotIn(error['message_id'], self.deleted())
        await self.command('/editar anterior 200,00', 903)
        self.assertIn(error['message_id'], self.deleted())
        self.assertIn(901, self.deleted())

    async def test_abandoned_calendar_retires_only_its_date_error(self):
        preview = self.new_preview()
        await self.command('/programar incorrecto', 900)
        error = self.notice('schedule-date')
        await self.click(f"p:{preview['id']}:schedule", preview['message_id'], photo=True)
        picker = self.store.current_picker(OWNER)
        await self.click(f"s:{picker['token']}:close:x", picker['message_id'], photo=True)
        self.assertIn(error['message_id'], self.deleted())
        self.assertIn(900, self.deleted())
        self.assertFalse(self.store.pending_all((OWNER,)))

    async def test_expired_draft_error_is_retired_only_when_new_draft_is_created(self):
        self.new_preview()
        self.now += self.store.ttl + 1
        await self.command('/previa', 900)
        error = self.notice('expired-draft')
        await self.command('/start', 901)
        self.assertNotIn(error['message_id'], self.deleted())
        self.bot.amazon.extract = AsyncMock(return_value=Product(URL, 'Nuevo producto'))
        await self.text(URL)
        self.assertIn(error['message_id'], self.deleted())
        self.assertIn(900, self.deleted())

    async def test_missing_reference_error_is_not_cleared_by_title_change(self):
        self.product.reference = ''
        self.store.replace_draft(OWNER, self.product, 1)
        self.store.update_draft(OWNER, self.product, self.photo())
        await self.bot.make_preview()
        preview = self.store.preview(OWNER)
        await self.edit_new(preview)
        await self.text(preview['caption'].replace('👉 ', '🔥 20 % de descuento\n\n👉 '))
        error = self.notice('percentage-reference')
        await self.command('/editar titulo Nuevo título', 900)
        self.assertNotIn(error['message_id'], self.deleted())
        await self.command('/editar anterior 200,00', 901)
        self.assertIn(error['message_id'], self.deleted())

    async def test_critical_review_deletion_retries_without_resending(self):
        post, notice = await self.failed_publication(uncertain=True)
        self.reset_api()
        def fail_delete(method, **kwargs):
            if method == 'deleteMessage': raise TelegramError(503)
            return True
        self.api.call.side_effect = fail_delete
        await self.click(f"n:{notice['token']}:review", notice['message_id'])
        self.assertFalse(self.notices())
        self.restart()
        self.now += 31
        self.api.call.side_effect = None
        self.reset_api()
        await self.bot.delivery.tick()
        self.assertIn(notice['message_id'], self.deleted())
        self.api.photo.assert_not_awaited()

    async def test_acknowledgement_connection_failure_keeps_critical_notice(self):
        post, notice = await self.failed_publication()
        def fail_answer(method, **kwargs):
            if method == 'answerCallbackQuery': raise TelegramError(503)
            return True
        self.api.call.side_effect = fail_answer
        await self.click(f"n:{notice['token']}:review", notice['message_id'])
        self.assertTrue(any(r['token'] == notice['token'] for r in self.notices()))
        self.api.call.side_effect = None
        await self.click(f"n:{notice['token']}:review", notice['message_id'])
        self.assertIn(notice['message_id'], self.deleted())

    async def test_missed_and_expired_publication_notices_also_require_review(self):
        for status in ['missed', 'expired']:
            with self.subTest(status=status):
                post = self.new_post()
                with self.store.connect() as db:
                    db.execute('UPDATE posts SET status=?,notice=? WHERE id=?', (status, 'No se publicó: ' + status, post['id']))
                await self.bot.delivery.notify()
                notice = self.notice('publication-result')
                self.assertEqual(notice['scope'], 'post:' + post['id'])
                self.assertEqual(notice['kind'], 'critical')
                self.api.photo.reset_mock()
                await self.click(f"n:{notice['token']}:review", notice['message_id'])
                self.assertEqual(self.store.get_post(post['id'])['status'], status)
                self.api.photo.assert_not_awaited()

    async def test_footer_configuration_is_verified_even_without_image_regeneration(self):
        preview = await self.generated()
        await self.edit_new(preview)
        with patch('canal_ofertas_bot.footer.parse_footer', side_effect=ValueError('PUBLICATION_FOOTER es demasiado larga para un único botón Copiar.')):
            await self.text(preview['caption'])
        error = self.notice('footer-configuration')
        await self.command('/start', 900)
        self.assertNotIn(error['message_id'], self.deleted())
        await self.text(preview['caption'])
        self.assertIn(error['message_id'], self.deleted())
        self.assertEqual(self.store.preview(OWNER)['photo'], preview['photo'])

    async def test_automatic_generation_error_belongs_to_newly_created_draft(self):
        self.new_preview()
        product = Product(URL, 'Nuevo producto', '29.99', image_url='https://images-eu.ssl-images-amazon.com/example.jpg')
        self.bot.amazon.extract = AsyncMock(return_value=product)
        self.bot.amazon.download = AsyncMock(return_value=self.photo())
        with patch('canal_ofertas_bot.service.render_card', side_effect=ValueError('Faltan las fuentes DejaVu dentro de la imagen; revisa FONT_DIR.')):
            await self.text(URL)
        error = self.notice('render-configuration')
        self.assertEqual(error['scope'], 'draft:' + self.store.draft(OWNER)['id'])
        await self.command('/previa', 900)
        self.assertIn(error['message_id'], self.deleted())

    async def test_late_cancellation_error_is_verified_after_post_history_is_pruned(self):
        from canal_ofertas_bot.notices import NoticeContext
        post = self.new_post()
        self.store.cancel_post(OWNER, post['id'])
        await self.bot.delivery.tick()
        self.assertIsNone(self.store.get_post(post['id']))
        await self.bot.notices.error(OWNER, 'La cancelación está guardada, pero no he podido actualizar su tarjeta. Consulta /programadas.',
                                    NoticeContext('post:' + post['id'], '/anular', input_id=900))
        notice = self.notice('cancel-update')
        self.reset_api()
        await self.bot.delivery.tick()
        self.assertIn('🚫 Cancelada', self.api.message.await_args.args[1])
        self.assertIn(notice['message_id'], self.deleted())
        self.assertIn(900, self.deleted())
        self.api.photo.assert_not_awaited()
        self.assertFalse(self.notices())

    async def test_reviewing_another_notice_does_not_verify_first_reviews_connection(self):
        first_post, first = await self.failed_publication()
        second_post, second = await self.failed_publication()
        def fail_answer(method, **kwargs):
            if method == 'answerCallbackQuery': raise TelegramError(503)
            return True
        self.api.call.side_effect = fail_answer
        await self.click(f"n:{first['token']}:review", first['message_id'])
        network = self.notice('operation:n:review')
        self.assertEqual(network['scope'], 'post:' + first_post['id'])
        self.api.call.side_effect = None
        await self.click(f"n:{second['token']}:review", second['message_id'])
        self.assertNotIn(network['message_id'], self.deleted())
        await self.click(f"n:{first['token']}:review", first['message_id'])
        self.assertIn(network['message_id'], self.deleted())

    async def test_unknown_publication_ids_keep_separate_notices_until_state_consultation(self):
        await self.command('/anular id-uno', 900)
        first = self.notice('unknown-post')
        await self.command('/anular id-dos', 901)
        second = self.notice('unknown-post')
        self.assertNotEqual(first['scope'], second['scope'])
        self.assertNotIn(first['message_id'], self.deleted())
        post = self.new_post()
        await self.command('/anular ' + post['id'], 902)
        self.assertNotIn(first['message_id'], self.deleted())
        self.assertNotIn(second['message_id'], self.deleted())
        await self.command('/programadas', 903)
        self.assertIn(first['message_id'], self.deleted())
        self.assertIn(second['message_id'], self.deleted())
        self.assertIn(900, self.deleted())
        self.assertIn(901, self.deleted())



class ProductWarnings(unittest.TestCase):
    def test_completed_manual_product_removes_extraction_instructions_but_keeps_conditions(self):
        p = Product(URL, 'Producto', '29.99', '39.99', warnings=[
            'Amazon solicita CAPTCHA. No se intenta eludirlo: completa los datos manualmente.',
            'Puedes completar todos los datos desde Telegram.',
            'Las promociones secundarias no se han restado del precio; comprueba sus condiciones.',
            'El precio de referencia puede ser PVP; no acredita el mínimo de 30 días.'])
        self.assertEqual(current_product_warnings(p, True), p.warnings[2:])
        self.assertEqual(current_product_warnings(p, False), p.warnings)

    def test_manual_reference_replaces_absence_warning_without_claiming_verification(self):
        p = Product(URL, 'Producto', '29.99', '39.99', warnings=['Sin referencia fiable: no se muestran referencia ni descuento.'])
        self.assertEqual(current_product_warnings(p, True), ['Referencia aportada manualmente; no acredita el mínimo de 30 días.'])
        p.reference = ''
        self.assertEqual(current_product_warnings(p, True), p.warnings)
