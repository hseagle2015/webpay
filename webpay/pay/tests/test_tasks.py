import calendar
import time
import urllib2

from django import test
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

import fudge
from fudge.inspector import arg
import jwt
import mock
from mock import ANY
from nose.exc import SkipTest
from nose.tools import eq_, raises
from requests.exceptions import RequestException, Timeout
import test_utils


from lib.marketplace.api import UnknownPricePoint
from lib.solitude import api
from lib.solitude import constants
from webpay.constants import TYP_CHARGEBACK, TYP_POSTBACK
from webpay.pay import tasks
from webpay.pay.models import Notice, SIMULATED_POSTBACK, SIMULATED_CHARGEBACK
from webpay.pay.samples import JWTtester

from .test_views import sample


class NotifyTest(JWTtester, test.TestCase):

    def setUp(self):
        super(NotifyTest, self).setUp()
        self.trans_uuid = 'some:uuid'

    def set_secret_mock(self, slumber, s):
        slumber.generic.product.get_object_or_404.return_value = {'secret': s}

    def url(self, path, protocol='https'):
        return protocol + '://' + self.domain + path


class TestNotifyApp(NotifyTest):

    @mock.patch('lib.solitude.api.client.get_transaction')
    def do_chargeback(self, reason, get_transaction):
        get_transaction.return_value = {
                'status': constants.STATUS_COMPLETED,
                'notes': {'pay_request': self.payload(),
                          'issuer_key': 'k'},
                'type': constants.TYPE_REFUND,
                'uuid': self.trans_uuid
        }
        with self.settings(INAPP_KEY_PATHS={None: sample}, DEBUG=True):
            tasks.chargeback_notify(self.trans_uuid, reason=reason)

    @mock.patch('lib.solitude.api.client.get_transaction')
    def notify(self, get_transaction):
        get_transaction.return_value = {
                'status': constants.STATUS_COMPLETED,
                'notes': {'pay_request': self.payload(),
                          'issuer_key': 'k'},
                'type': constants.TYPE_PAYMENT,
                'uuid': self.trans_uuid
        }
        with self.settings(INAPP_KEY_PATHS={None: sample}, DEBUG=True):
            tasks.payment_notify('some:uuid')

    @fudge.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    def test_notify_pay(self, fake_req, slumber):
        self.set_secret_mock(slumber, 'f')
        payload = self.payload(typ=TYP_POSTBACK)
        url = payload['request']['postbackURL']

        def req_ok(req):
            dd = jwt.decode(req['notice'], verify=False)
            eq_(dd['request'], payload['request'])
            eq_(dd['typ'], payload['typ'])
            jwt.decode(req['notice'], 'f', verify=True)
            return True

        (fake_req.expects('post').with_args(url, arg.passes_test(req_ok),
                                            timeout=5)
                                 .returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .expects('raise_for_status'))
        self.notify()
        notice = Notice.objects.get()
        eq_(notice.transaction_uuid, self.trans_uuid)
        eq_(notice.success, True)
        eq_(notice.url, url)

    @fudge.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    def test_notify_refund_chargeback(self, fake_req, slumber):
        self.set_secret_mock(slumber, 'f')
        payload = self.payload(typ=TYP_CHARGEBACK)
        url = payload['request']['chargebackURL']

        def req_ok(req):
            dd = jwt.decode(req['notice'], verify=False)
            eq_(dd['request'], payload['request'])
            eq_(dd['typ'], payload['typ'])
            eq_(dd['response']['transactionID'], self.trans_uuid)
            eq_(dd['response']['reason'], 'refund')
            jwt.decode(req['notice'], 'f', verify=True)
            return True

        (fake_req.expects('post').with_args(url, arg.passes_test(req_ok),
                                            timeout=5)
                                 .returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .expects('raise_for_status'))
        self.do_chargeback('refund')
        notice = Notice.objects.get()
        eq_(notice.transaction_uuid, self.trans_uuid)
        eq_(notice.success, True)
        eq_(notice.url, url)

    @fudge.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    def test_notify_reversal_chargeback(self, fake_req, slumber):
        self.set_secret_mock(slumber, 'f')

        def req_ok(req):
            dd = jwt.decode(req['notice'], verify=False)
            eq_(dd['response']['reason'], 'reversal')
            return True

        (fake_req.expects('post').with_args('http://foo.url/charge',
                                            arg.passes_test(req_ok),
                                            timeout=5)
                                 .returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .expects('raise_for_status'))
        self.do_chargeback('reversal')
        notice = Notice.objects.get()
        eq_(notice.transaction_uuid, self.trans_uuid)
        eq_(notice.last_error, '')
        eq_(notice.success, True)

    @mock.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_notify_marketplace(self, marketplace, solitude, requests):
        self.set_secret_mock(solitude, 'f')
        requests.post.side_effect = Timeout('Timeout')
        self.notify()
        assert marketplace.webpay.failure.called

    @mock.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_notify_timeout(self, marketplace, solitude, requests):
        self.set_secret_mock(solitude, 'f')
        requests.post.side_effect = Timeout('Timeout')
        self.notify()
        notice = Notice.objects.get()
        eq_(notice.success, False)
        er = notice.last_error
        assert 'Timeout' in er, 'Unexpected: %s' % er

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('webpay.pay.tasks.payment_notify.retry')
    @mock.patch('webpay.pay.utils.requests.post')
    def test_retry_http_error(self, post, retry, slumber):
        self.set_secret_mock(slumber, 'f')
        post.side_effect = RequestException('500 error')
        self.notify()
        assert post.called, 'notification not sent'
        assert retry.called, 'task was not retried after error'

    @fudge.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_any_error(self, fake_req, marketplace, solitude):
        self.set_secret_mock(solitude, 'f')
        fake_req.expects('post').raises(RequestException('some http error'))
        self.notify()
        notice = Notice.objects.get()
        eq_(notice.success, False)
        er = notice.last_error
        assert 'some http error' in er, 'Unexpected: %s' % er

    @fudge.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_bad_status(self, fake_req, marketplace, solitude):
        self.set_secret_mock(solitude, 'f')
        (fake_req.expects('post').returns_fake()
                                 .has_attr(text='')
                                 .expects('raise_for_status')
                                 .raises(urllib2.HTTPError('url', 500, 'Error',
                                                           [], None)))
        self.notify()
        notice = Notice.objects.get()
        eq_(notice.success, False)
        er = notice.last_error
        assert 'HTTP Error' in er, 'Unexpected: %s' % er

    @fudge.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    def test_invalid_app_response(self, fake_req, slumber):
        self.set_secret_mock(slumber, 'f')
        (fake_req.expects('post').returns_fake()
                                 .provides('raise_for_status')
                                 .has_attr(text='<not a valid response>'))
        self.notify()
        notice = Notice.objects.get()
        eq_(notice.success, False)

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('webpay.pay.utils.requests')
    @mock.patch('webpay.pay.tasks.payment_notify.retry')
    def test_notify_retries(self, retry, requests, slumber):
        self.set_secret_mock(slumber, 'f')
        requests.post.side_effect = RequestException('some http error')
        self.notify()
        assert retry.called, 'task was not retried after error'

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('webpay.pay.utils.requests')
    @mock.patch('webpay.pay.utils.notify_failure')
    def test_failure_notifies(self, notify, requests, slumber):
        self.set_secret_mock(slumber, 'f')
        requests.post.side_effect = RequestException('some http error')
        self.notify()
        assert notify.called, 'notify called'

    @fudge.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    def test_signed_app_response(self, fake_req, slumber):
        app_payment = self.payload()
        self.set_secret_mock(slumber, 'f')
        slumber.generic.product.get_object_or_404.return_value = {
            'secret': 'f'}

        # Ensure that the JWT sent to the app for payment notification
        # includes the same payment data that the app originally sent.
        def is_valid(payload):
            data = jwt.decode(payload['notice'], 'f',  # secret key
                              verify=True)
            eq_(data['iss'], settings.NOTIFY_ISSUER)
            eq_(data['typ'], TYP_POSTBACK)
            eq_(data['request']['pricePoint'], 1)
            eq_(data['request']['name'], app_payment['request']['name'])
            eq_(data['request']['description'],
                app_payment['request']['description'])
            eq_(data['request']['productdata'],
                app_payment['request']['productdata'])
            eq_(data['request']['postbackURL'], 'http://foo.url/post')
            eq_(data['request']['chargebackURL'], 'http://foo.url/charge')
            eq_(data['response']['transactionID'], 'some:uuid')
            assert data['iat'] <= calendar.timegm(time.gmtime()) + 60, (
                                'Expected iat to be about now')
            assert data['exp'] > calendar.timegm(time.gmtime()) + 3500, (
                                'Expected exp to be about an hour from now')
            return True

        (fake_req.expects('post').with_args(arg.any(),
                                            arg.passes_test(is_valid),
                                            timeout=arg.any())
                                 .returns_fake()
                                 .has_attr(text='<not a valid response>')
                                 .provides('raise_for_status'))
        self.notify()


@mock.patch('lib.solitude.api.client.slumber')
class TestSimulatedNotifications(NotifyTest):

    def notify(self, payload):
        tasks.simulate_notify('issuer-key', payload,
                              trans_uuid=self.trans_uuid)

    @fudge.patch('webpay.pay.utils.requests')
    def test_postback(self, slumber, fake_req):
        self.set_secret_mock(slumber, 'f')
        payload = self.payload(typ=TYP_POSTBACK,
                               extra_req={'simulate': {'result': 'postback'}})
        url = payload['request']['postbackURL']

        def req_ok(req):
            dd = jwt.decode(req['notice'], verify=False)
            eq_(dd['request'], payload['request'])
            eq_(dd['typ'], payload['typ'])
            jwt.decode(req['notice'], 'f', verify=True)
            return True

        (fake_req.expects('post').with_args(url, arg.passes_test(req_ok),
                                            timeout=arg.any())
                                 .returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .expects('raise_for_status'))
        self.notify(payload)
        notice = Notice.objects.get()
        assert notice.transaction_uuid, notice
        eq_(notice.success, True)
        eq_(notice.url, url)
        eq_(notice.simulated, SIMULATED_POSTBACK)

    @fudge.patch('webpay.pay.utils.requests')
    def test_chargeback(self, slumber, fake_req):
        self.set_secret_mock(slumber, 'f')
        req = {'simulate': {'result': 'chargeback'}}
        payload = self.payload(typ=TYP_CHARGEBACK,
                               extra_req=req)
        url = payload['request']['chargebackURL']

        def req_ok(req):
            dd = jwt.decode(req['notice'], verify=False)
            eq_(dd['request'], payload['request'])
            eq_(dd['typ'], payload['typ'])
            jwt.decode(req['notice'], 'f', verify=True)
            return True

        (fake_req.expects('post').with_args(url, arg.passes_test(req_ok),
                                            timeout=arg.any())
                                 .returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .expects('raise_for_status'))
        self.notify(payload)
        notice = Notice.objects.get()
        assert notice.transaction_uuid, notice
        eq_(notice.success, True)
        eq_(notice.url, url)
        eq_(notice.simulated, SIMULATED_CHARGEBACK)

    @fudge.patch('webpay.pay.utils.requests')
    def test_chargeback_reason(self, slumber, fake_req):
        self.set_secret_mock(slumber, 'f')
        reason = 'something'
        req = {'simulate': {'result': 'chargeback',
                            'reason': reason}}
        payload = self.payload(typ=TYP_CHARGEBACK,
                               extra_req=req)
        url = payload['request']['chargebackURL']

        def req_ok(req):
            dd = jwt.decode(req['notice'], verify=False)
            eq_(dd['request'], payload['request'])
            eq_(dd['response']['reason'], reason)
            return True

        (fake_req.expects('post').with_args(url, arg.passes_test(req_ok),
                                            timeout=arg.any())
                                 .returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .expects('raise_for_status'))
        self.notify(payload)

    @mock.patch('webpay.pay.tasks.simulate_notify.retry')
    @mock.patch('webpay.pay.utils.requests.post')
    def test_retry_http_error(self, post, retry, slumber):
        self.set_secret_mock(slumber, 'f')
        post.side_effect = RequestException('500 error')

        req = {'simulate': {'result': 'postback'}}
        payload = self.payload(typ=TYP_POSTBACK,
                               extra_req=req)
        self.notify(payload)

        assert post.called, 'notification not sent'
        assert retry.called, 'task was not retried after error'
        retry.assert_called_with(args=['issuer-key', payload],
                                 max_retries=ANY, eta=ANY, exc=ANY)

    @mock.patch('webpay.pay.utils.requests.post')
    @mock.patch('webpay.pay.utils.notify_failure')
    def test_no_notifications_on_simulate(self, notify_failure, post, slumber):
        self.set_secret_mock(slumber, 'f')
        post.side_effect = RequestException('500 error')

        req = {'simulate': {'result': 'postback'}}
        payload = self.payload(typ=TYP_POSTBACK, extra_req=req)
        self.notify(payload)
        assert not notify_failure.called, 'Notification should not be sent'


class TestStartPay(test_utils.TestCase):

    def setUp(self):
        self.issue = 'some-seller-uuid'
        self.user_uuid = 'some-user-uuid'
        self.transaction_uuid = 'webpay:some-id'
        self.notes = {'issuer_key': self.issue,
                      'pay_request': {
                            'iss': 'some-seller-key',
                            'request': {'pricePoint': 1,
                                        'id': 'generated-product-uuid',
                                        'icons': {'64': 'http://app/i.png'},
                                        'name': 'Virtual Sword'}}}
        self.prices = {'prices': [{'amount': 1, 'currency': 'EUR'}]}

    @mock.patch('lib.solitude.api.client.get_transaction')
    @mock.patch('lib.marketplace.api.client.api')
    def start(self, marketplace, solitude):
        prices = mock.Mock()
        prices.get_object.return_value = self.prices
        marketplace.webpay.prices.return_value = prices
        solitude.get_transaction.return_value = {
                'status': constants.STATUS_COMPLETED,
                'notes': self.notes,
                'type': constants.TYPE_PAYMENT,
                'uuid': self.transaction_uuid
        }
        tasks.start_pay(self.transaction_uuid, self.notes, self.user_uuid)

    def set_billing_id(self, slumber, num):
        slumber.bango.billing.post.return_value = {
            'resource_pk': '3333',
            'billingConfigurationId': num,
            'responseMessage': 'Success',
            'responseCode': 'OK',
            'resource_uri': '/bango/billing/3333/'
        }

    @raises(api.SellerNotConfigured)
    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_no_seller(self, marketplace, solitude):
        raise SkipTest
        marketplace.webpay.prices.return_value = self.prices
        solitude.generic.seller.get.return_value = {'meta': {'total_count': 0}}
        self.start()
        #eq_(self.get_trans().status, TRANS_STATE_FAILED)

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_transaction_called(self, marketplace, solitude):
        solitude.generic.transaction.get_object.return_value = {
            'status': 'not-pending',
            'resource_pk': 5}
        self.start()
        solitude.generic.transaction.assert_called_with(5)

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_price_used(self, marketplace, solitude):
        prices = mock.Mock()
        prices.get.return_value = self.prices
        marketplace.webpay.prices.return_value = prices
        self.set_billing_id(solitude, 123)
        self.start()
        eq_(solitude.bango.billing.post.call_args[0][0]['prices'],
            self.prices['prices'])

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('webpay.pay.tasks.get_icon_url')
    @mock.patch('lib.marketplace.api.client.api')
    def test_icon_url_sent(self, mkt, get_icon_url, solitude):
        url = 'http://mkt-cdn/media/icon.png'
        get_icon_url.return_value = url
        self.start()
        eq_(solitude.bango.billing.post.call_args[0][0]['icon_url'], url)

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('webpay.pay.tasks.get_icon_url')
    @mock.patch('lib.marketplace.api.client.api')
    def test_icon_url_disabled(self, mkt, get_icon_url, solitude):
        with self.settings(USE_PRODUCT_ICONS=False):
            self.start()
        eq_(solitude.bango.billing.post.call_args[0][0]['icon_url'], None)
        assert not get_icon_url.called

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('webpay.pay.tasks.get_icon_url')
    @mock.patch('lib.marketplace.api.client.api')
    def test_catch_icon_exceptions(self, mkt, get_icon_url, solitude):
        get_icon_url.side_effect = ValueError('just some exception')
        self.start()

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('webpay.pay.tasks.mkt_client.get_price')
    def test_price_fails(self, get_price, solitude):
        get_price.side_effect = UnknownPricePoint
        with self.assertRaises(UnknownPricePoint):
            self.start()

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    @raises(RuntimeError)
    def test_exception_fails_transaction(self, marketplace, slumber):
        raise SkipTest
        slumber.generic.seller.get.side_effect = RuntimeError
        self.start()
        #trans = self.get_trans()
        # Currently solitude doesn't have the concept of a failed transaction.
        # Perhaps we should add that?
        #eq_(trans.status, TRANS_STATE_FAILED)

    @mock.patch.object(settings, 'KEY', 'marketplace-domain')
    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_marketplace_seller_switch(self, marketplace, solitude):
        marketplace.webpay.prices.get.return_value = self.prices
        self.set_billing_id(solitude, 123)

        # Simulate how the Marketplace would add
        # a custom seller_uuid to the product data in the JWT.
        app_seller_uuid = 'some-seller-uuid'
        data = 'seller_uuid=%s' % app_seller_uuid
        self.notes['issuer_key'] = 'marketplace-domain'
        self.notes['pay_request']['request']['productData'] = data
        self.start()

        # Check that the seller_uuid was switched to that of the app seller.
        solitude.generic.seller.get_object.assert_called_with(
            uuid=app_seller_uuid)

    @raises(ValueError)
    @mock.patch.object(settings, 'KEY', 'marketplace-domain')
    @mock.patch('lib.solitude.api.client.api')
    @mock.patch('webpay.pay.tasks.client')
    def test_marketplace_missing_seller_uuid(self, cli, slumber):
        self.notes['issuer_key'] = settings.KEY
        self.notes['pay_request']['request']['productData'] = 'foo-bar'
        self.start()

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_prevent_restarting_received(self, marketplace, solitude):
        solitude.generic.transaction.get_object.return_value = {
            'status': constants.STATUS_RECEIVED,
            'resource_pk': '1'}
        self.start()
        assert not solitude.generic.transaction.called

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_prevent_restarting_pending(self, marketplace, solitude):
        solitude.generic.transaction.get_object.return_value = {
            'status': constants.STATUS_PENDING,
            'resource_pk': '1'}
        self.start()
        assert not solitude.generic.transaction.called


class TestGetIconURL(test_utils.TestCase):

    def setUp(self):
        p = mock.patch('lib.marketplace.api.client.api')
        self.marketplace = p.start()
        self.addCleanup(p.stop)
        self.request = {'icons': {'64': 'http://app/icon.png'}}
        self.size = 64
        p = mock.patch.object(settings, 'PRODUCT_ICON_SIZE', self.size)
        p.start()
        self.addCleanup(p.stop)

    def get_icon_url(self):
        return tasks.get_icon_url(self.request)

    def test_get_url_from_api(self):
        url = 'http://mkt-cdn/media/icon.png'
        icon = {'url': url}
        self.marketplace.webpay.product.icon.get_object.return_value = icon
        eq_(self.get_icon_url(), url)

    def test_no_cached_icon(self):
        icon = self.marketplace.webpay.product.icon
        icon.get_object.side_effect = ObjectDoesNotExist()
        eq_(self.get_icon_url(), None)
        post = self.marketplace.webpay.product.icon.post
        post.assert_called_with(dict(ext_url=self.request['icons']['64'],
                                     size=64, ext_size=64))

    def test_no_app_icons(self):
        del self.request['icons']
        eq_(self.get_icon_url(), None)

    def test_empty_app_icons(self):
        self.request['icons'] = {}
        eq_(self.get_icon_url(), None)

    def test_use_largest(self):
        self.request = {'icons': {'128': 'http://app/128.png',
                                  '512': 'http://app/512.png'}}
        self.get_icon_url()
        get = self.marketplace.webpay.product.icon.get_object
        get.assert_called_with(ext_url=self.request['icons']['512'],
                               size=self.size, ext_size='512')

    def test_use_exact(self):
        self.request = {'icons': {'64': 'http://app/64.png',
                                  '512': 'http://app/512.png'}}
        self.get_icon_url()
        get = self.marketplace.webpay.product.icon.get_object
        get.assert_called_with(ext_url=self.request['icons']['64'],
                               size=64, ext_size=64)

    def test_icon_too_small_to_resize(self):
        self.request = {'icons': {'48': 'http://app/48.png'}}
        self.get_icon_url()
        get = self.marketplace.webpay.product.icon.get_object
        get.assert_called_with(ext_url=self.request['icons']['48'],
                               size='48', ext_size='48')


class TestConfigureTrans(test.TestCase):

    def configure(self):
        tasks.configure_transaction(self.request)

    def setUp(self):
        self.request = mock.Mock()
        sess = {}
        self.request.session = sess
        sess['is_simulation'] = False
        sess['notes'] = {}
        sess['trans_id'] = 'trans-id'
        sess['uuid'] = 'some-email-token'
        patch = mock.patch('webpay.pay.tasks.start_pay')
        self.start_pay = patch.start()
        self.patches = [patch]

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_configure(self):
        self.configure()
        assert self.start_pay.delay.called

    def test_skip_when_fake(self):
        with self.settings(FAKE_PAYMENTS=True):
            self.configure()
        assert not self.start_pay.delay.called

    def test_skip_when_simulating(self):
        self.request.session['is_simulation'] = True
        self.configure()
        assert not self.start_pay.delay.called
