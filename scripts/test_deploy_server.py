import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    'deploy_server', Path(__file__).parent / 'deployment' / 'deploy-server.py')
deploy_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy_server)


def make_archive(path, files, extra=None):
    with tarfile.open(path, 'w:gz') as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = self.root / 'release.tar.gz'
        self.target = self.root / 'release'
        self.target.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_accepts_only_exact_deploy_command(self):
        sha, digest = 'a' * 40, 'b' * 64
        self.assertEqual(deploy_server.command(f'deploy {sha} {digest}'), (sha, digest))
        for bad in ['', 'deploy', f'deploy {sha}', f'deploy {sha} {digest} x', f'deploy {sha}; id {digest}']:
            with self.assertRaises(ValueError):
                deploy_server.command(bad)

    def test_extracts_site_files(self):
        make_archive(self.archive, {'./index.html': b'<html></html>', './assets/a.png': b'x',
                                    './release.json': b'{}', './.well-known/security.txt': b'x'})
        deploy_server.extract(self.archive, self.target)
        self.assertTrue((self.target / 'assets/a.png').is_file())
        self.assertTrue((self.target / '.well-known/security.txt').is_file())

    def test_rejects_hidden_files_traversal_and_links(self):
        for name in ['.git/config', 'assets/.env', '../escape', '/etc/passwd']:
            make_archive(self.archive, {name: b'x'})
            with self.assertRaises(ValueError, msg=name):
                deploy_server.extract(self.archive, self.target)
        link = tarfile.TarInfo('assets/link')
        link.type = tarfile.SYMTYPE
        link.linkname = '/etc/passwd'
        make_archive(self.archive, {}, [link])
        with self.assertRaises(ValueError):
            deploy_server.extract(self.archive, self.target)


if __name__ == '__main__':
    unittest.main()
