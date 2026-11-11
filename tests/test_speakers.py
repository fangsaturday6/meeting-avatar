import numpy as np

from avatar.speakers import SpeakerBook


def _cluster(rng, center, n, noise=0.15):
    return [center + rng.normal(0, noise, center.shape) / np.sqrt(center.size) * 4 for _ in range(n)]


def test_two_voices_become_two_stable_speakers():
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=192), rng.normal(size=192)
    book = SpeakerBook()
    ids = []
    for ea, eb in zip(_cluster(rng, a, 6), _cluster(rng, b, 6)):
        ids.append((book.assign(ea), book.assign(eb)))
    assert len(book.speakers) == 2
    assert len({i for i, _ in ids}) == 1 and len({j for _, j in ids}) == 1
    assert ids[0][0] != ids[0][1]


def test_missing_embedding_reuses_last_speaker():
    rng = np.random.default_rng(1)
    book = SpeakerBook()
    first = book.assign(rng.normal(size=192))
    assert book.assign(None) == first
    assert book.speakers[first].count == 2


def test_missing_embedding_with_no_history_creates_one():
    book = SpeakerBook()
    assert book.assign(None) == 1


def test_rename_and_by_name():
    book = SpeakerBook()
    sid = book.assign(np.ones(8))
    book.rename(sid, "张三")
    assert book.name_of(sid) == "张三"
    assert book.by_name("张三") == sid
    other = book.by_name("李四")
    assert other != sid and book.name_of(other) == "李四"
    assert book.name_of(None) == "有人"


def test_centroid_follows_new_samples():
    book = SpeakerBook()
    v = np.zeros(8); v[0] = 1
    sid = book.assign(v)
    w = np.zeros(8); w[0], w[1] = 1, 0.6
    assert book.assign(w) == sid
    assert book.speakers[sid].centroid[1] > 0
