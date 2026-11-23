import unittest
import urllib.parse
from unittest import mock

import stubs  # noqa: F401
import kodistubs
from clouddrive.common.exception import ExceptionUtils, RequestException
from clouddrive.common.remote.provider import Provider
from resources.lib.provider.pcloud import PCloud
from urllib.error import HTTPError

kodistubs.use_temp_profile()

TOKENS = {'access_token': 'AT', 'refresh_token': '', 'expires_in': PCloud._NEVER_EXPIRES_S, 'date': 0, 'hostname': 'eapi.pcloud.com'}


def folder(folderid, name, parent=0, contents=None):
    entry = {'id': 'd%d' % folderid, 'folderid': folderid, 'name': name, 'isfolder': True, 'parentfolderid': parent, 'modified': 1730000000}
    if contents is not None:
        entry['contents'] = contents
    return entry


def file(fileid, name, parent, category=0, **extra):
    return dict({'id': 'f%d' % fileid, 'fileid': fileid, 'name': name, 'isfolder': False, 'parentfolderid': parent,
                 'modified': 1730000000, 'size': 10, 'category': category, 'contenttype': 'video/x-matroska'}, **extra)


MOVIES = folder(5, 'Movies', 0, [file(51, 'Movie.mkv', 5, 2, width=1920, height=1080, duration='5400.5'), file(52, 'Movie.en.srt', 5),
                                 file(53, 'Movie.srt', 5), file(54, 'Movie2.srt', 5)])


class FakeRequest(object):
    def __init__(self, answer):
        self.answer = answer

    def request_json(self):
        return self.answer


class FakePCloud(PCloud):
    """Answers API calls from `responses`, keyed by (method, folderid/fileid/query) or by method."""

    def __init__(self, responses, tokens=None):
        super(FakePCloud, self).__init__()
        self.responses = responses
        self.tokens = dict(TOKENS if tokens is None else tokens)
        self.calls = []
        self.cancelled = False

    def get_access_tokens(self):
        return self.tokens

    def prepare_request(self, method, path, parameters=None, request_params=None, access_tokens=None, headers=None):
        url = urllib.parse.urlparse(path)
        api_method = url.path.lstrip('/')
        self.calls.append((method, url.netloc, api_method, dict(parameters or {})))
        params = parameters or {}
        key = params.get('folderid', params.get('fileid', params.get('query')))
        answer = self.responses.get((api_method, key), self.responses.get(api_method))
        if isinstance(answer, Exception):
            raise answer
        return FakeRequest(answer)

    def cancel_operation(self):
        return self.cancelled


def status(e):
    return ExceptionUtils.extract_exception(e, HTTPError).code


class PCloudProviderTest(unittest.TestCase):
    def test_tokens_from_the_sign_in_server_never_expire(self):
        with mock.patch.object(Provider, 'fetch_tokens_info', return_value={'access_token': 'AT', 'hostname': 'eapi.pcloud.com', 'date': 1}):
            tokens = PCloud().fetch_tokens_info({})
        self.assertEqual(tokens['refresh_token'], '')
        self.assertGreater(tokens['expires_in'], 50 * 365 * 24 * 3600)
        self.assertEqual(tokens['hostname'], 'eapi.pcloud.com')

    def test_refreshing_asks_to_sign_in_again(self):
        with self.assertRaises(RequestException) as raised:
            PCloud().refresh_access_tokens()
        self.assertEqual(status(raised.exception), 401)

    def test_calls_go_to_the_account_s_region_with_timestamps(self):
        p = FakePCloud({'userinfo': {'result': 0, 'userid': 42, 'email': 'me@example.com', 'premium': True}})
        self.assertEqual(p.get_account(), {'id': '42', 'name': 'me@example.com'})
        self.assertEqual(p.calls[0][:3], ('get', 'eapi.pcloud.com', 'userinfo'))
        self.assertEqual(p.calls[0][3]['timeformat'], 'timestamp')
        self.assertEqual(p.get_drives(), [{'id': '42', 'name': '', 'type': 'premium'}])
        self.assertEqual(p.get_drive_type_name('premium'), 'pCloud Premium')

    def test_a_new_account_is_read_with_the_tokens_just_received(self):
        # While an account is being added, the provider has no account manager: only the tokens passed in.
        sent = []

        def request_json(request):
            sent.append((request.url, request.headers['authorization']))
            return {'result': 0, 'userid': 7, 'email': 'new@example.com'}

        with mock.patch('clouddrive.common.remote.request.Request.request_json', request_json):
            account = PCloud().get_account(access_tokens=dict(TOKENS, date=10 ** 10))
        self.assertEqual(account, {'id': '7', 'name': 'new@example.com'})
        self.assertTrue(sent[0][0].startswith('https://eapi.pcloud.com/userinfo?'))
        self.assertEqual(sent[0][1], 'Bearer AT')

    def test_only_pcloud_hosts_are_used(self):
        for hostname in (None, 'evil.example.com'):
            p = FakePCloud({'userinfo': {'result': 0, 'userid': 1}}, dict(TOKENS, hostname=hostname))
            p.get_account()
            self.assertEqual(p.calls[0][1], 'api.pcloud.com')

    def test_result_codes_become_http_errors(self):
        for result, code in ((2094, 401), (1000, 401), (2005, 404), (4000, 503), (5000, 503), (2003, 400)):
            p = FakePCloud({'userinfo': {'result': result, 'error': 'Nope'}})
            with self.assertRaises(RequestException) as raised:
                p.get_account()
            self.assertEqual(status(raised.exception), code, result)
            self.assertIn('Nope', raised.exception.response)

    def test_the_root_folder_lists_folders_and_files(self):
        p = FakePCloud({('listfolder', '0'): {'result': 0, 'metadata': folder(0, '/', None, [folder(5, 'Movies'), file(9, 'song.mp3', 0, 3, artist='A', title='T', trackno='3')])}})
        items = p.get_folder_items()
        self.assertEqual([i['id'] for i in items], ['d5', 'f9'])
        self.assertEqual({i['parent'] for i in items}, {'d0'})
        self.assertIn('folder', items[0])
        self.assertEqual(items[1]['audio'], {'artist': 'A', 'title': 'T', 'tracknumber': '3'})
        self.assertEqual(items[1]['last_modified_date'], '2024-10-27T03:33:20Z')

    def test_listings_ask_for_no_download_links(self):
        p = FakePCloud({('listfolder', '5'): {'result': 0, 'metadata': MOVIES}})
        movie = p.get_folder_items(item_id='d5', include_download_info=True)[0]
        self.assertEqual(movie['video'], {'width': 1920, 'height': 1080, 'duration': 5400})
        self.assertNotIn('download_info', movie)
        self.assertEqual([c[2] for c in p.calls], ['listfolder'])

    def test_exports_get_links_for_the_files_they_download(self):
        # The export service downloads .strm/.nomedia/.nfo and artwork from the listing's download_info.
        tree_answer = folder(5, 'Movies', 0, [file(51, 'Movie.mkv', 5), file(52, 'Movie.nfo', 5), file(53, 'Movie-poster.jpg', 5),
                                              file(54, 'Holiday.jpg', 5), folder(6, 'extrafanart', 5, [file(61, 'a.jpg', 6)])])
        links = {('getfilelink', fid): {'result': 0, 'hosts': ['c.pcloud.com'], 'path': '/%s' % fid} for fid in ('51', '52', '53', '54', '61')}
        p = FakePCloud(dict(list(links.items()) + [(('listfolder', '5'), {'result': 0, 'metadata': tree_answer})]))
        tree = p.list_subtree('42', 'd5')
        with_link = sorted(i['id'] for items in tree.values() for i in items if 'download_info' in i)
        self.assertEqual(with_link, ['f52', 'f53', 'f61'])
        self.assertEqual([c[3]['fileid'] for c in p.calls if c[2] == 'getfilelink'], ['52', '53', '61'])
        p = FakePCloud(dict(list(links.items()) + [(('listfolder', '5'), {'result': 0, 'metadata': tree_answer})]))
        listed = p.get_folder_items(item_id='d5', include_download_info=True)
        self.assertEqual(sorted(i['id'] for i in listed if 'download_info' in i), ['f52', 'f53'])
        p = FakePCloud({('listfolder', '5'): {'result': 0, 'metadata': tree_answer}})
        self.assertFalse(any('download_info' in i for i in p.get_folder_items(item_id='d5')))

    def test_a_file_plays_from_a_pre_authenticated_link(self):
        p = FakePCloud({('stat', '51'): {'result': 0, 'metadata': MOVIES['contents'][0]},
                        ('getfilelink', '51'): {'result': 0, 'hosts': ['c1.pcloud.com', 'c2.pcloud.com'], 'path': '/dl/abc/Movie.mkv'}})
        self.assertEqual(p.get_item(item_id='f51', include_download_info=True)['download_info']['url'], 'https://c1.pcloud.com/dl/abc/Movie.mkv')
        self.assertFalse(p.download_requires_auth)

    def test_the_source_service_names_items_by_path(self):
        p = FakePCloud({'listfolder': {'result': 0, 'metadata': MOVIES}, 'stat': {'result': 0, 'metadata': MOVIES['contents'][0]}})
        self.assertEqual(len(p.get_folder_items(path='/Movies/')), 4)
        self.assertEqual(p.get_item(path='/Movies/Movie.mkv')['id'], 'f51')
        self.assertEqual([c[3].get('path') for c in p.calls], ['/Movies', '/Movies/Movie.mkv'])
        p = FakePCloud({('listfolder', '0'): {'result': 0, 'metadata': folder(0, '/', None, [])}})
        p.get_folder_items(path='/')
        self.assertEqual(p.calls[0][3]['folderid'], '0')

    def test_subtitles_are_named_after_the_video(self):
        p = FakePCloud({('listfolder', '5'): {'result': 0, 'metadata': MOVIES}})
        self.assertEqual([s['name'] for s in p.get_subtitles('d5', 'Movie.mkv')], ['Movie.en.srt', 'Movie.srt'])

    def test_a_file_is_read_with_stat_and_gets_its_subtitles(self):
        p = FakePCloud({('stat', '51'): {'result': 0, 'metadata': MOVIES['contents'][0]}, ('listfolder', '5'): {'result': 0, 'metadata': MOVIES}})
        item = p.get_item(item_id='f51', find_subtitles=True)
        self.assertEqual((item['name'], item['parent']), ('Movie.mkv', 'd5'))
        self.assertEqual([s['id'] for s in item['subtitles']], ['f52', 'f53'])

    def test_the_root_folder_item(self):
        p = FakePCloud({('listfolder', '0'): {'result': 0, 'metadata': folder(0, '/', None, [])}})
        item = p.get_item()
        self.assertEqual((item['id'], item['name'], item['parent']), ('d0', '', None))

    def test_exports_list_a_folder_tree_in_one_call(self):
        tree_answer = folder(5, 'Movies', 0, [file(51, 'Movie.mkv', 5), folder(6, 'Extras', 5, [file(61, 'Trailer.mkv', 6)])])
        p = FakePCloud({('listfolder', '5'): {'result': 0, 'metadata': tree_answer}})
        tree = p.list_subtree('42', 'd5')
        self.assertEqual({k: [i['id'] for i in v] for k, v in tree.items()}, {'d5': ['f51', 'd6'], 'd6': ['f61']})
        self.assertEqual([c[3].get('recursive') for c in p.calls if c[2] == 'listfolder'], [1])

    def test_the_root_tree_is_listed_folder_by_folder(self):
        # pCloud refuses a recursive listing of the root (error 1101).
        p = FakePCloud({('listfolder', '0'): {'result': 0, 'metadata': folder(0, '/', None, [folder(5, 'Movies'), file(9, 'a.mkv', 0)])},
                        ('listfolder', '5'): {'result': 0, 'metadata': folder(5, 'Movies', 0, [file(51, 'Movie.mkv', 5)])}})
        tree = p.list_subtree('42', None)
        self.assertEqual({k: [i['id'] for i in v] for k, v in tree.items()}, {'d0': ['f9', 'd5'], 'd5': ['f51']})
        self.assertEqual([(c[3]['folderid'], c[3].get('recursive')) for c in p.calls if c[2] == 'listfolder'], [('0', None), ('5', 1)])

    def test_search_uses_pcloud_s_search(self):
        p = FakePCloud({('search', 'movie'): {'result': 0, 'items': MOVIES['contents'][:2]}})
        self.assertEqual([i['id'] for i in p.search('movie')], ['f51', 'f52'])

    def test_search_in_a_folder_keeps_what_is_inside(self):
        p = FakePCloud({('search', 'movie'): {'result': 0, 'items': [file(51, 'Movie.mkv', 5), file(99, 'Movie.avi', 9)]},
                        ('listfolder', '5'): {'result': 0, 'metadata': MOVIES}})
        self.assertEqual([i['id'] for i in p.search('movie', item_id='d5')], ['f51'])

    def test_search_walks_the_folders_without_pcloud_s_search(self):
        p = FakePCloud({('search', 'MOVIE'): {'result': 2003, 'error': 'Access denied.'},
                        ('listfolder', '0'): {'result': 0, 'metadata': folder(0, '/', None, [folder(5, 'Movies')])},
                        ('listfolder', '5'): {'result': 0, 'metadata': MOVIES}})
        self.assertEqual([i['name'] for i in p.search('MOVIE')], ['Movies', 'Movie.mkv', 'Movie.en.srt', 'Movie.srt', 'Movie2.srt'])


if __name__ == '__main__':
    unittest.main()
