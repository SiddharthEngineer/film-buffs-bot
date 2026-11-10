from filmbuffs.tmdb import Movie, choice_label, parse_choice


def test_parse_choice_accepts_autocomplete_values():
    assert parse_choice("tmdb:603") == 603


def test_parse_choice_accepts_tmdb_urls():
    assert parse_choice("https://www.themoviedb.org/movie/603-the-matrix") == 603


def test_parse_choice_rejects_free_text_including_numeric_titles():
    assert parse_choice("The Matrix") is None
    assert parse_choice("1917") is None   # a real movie title, not an ID
    assert parse_choice("tmdb:abc") is None
    assert parse_choice("") is None


def test_choice_label_format_and_length():
    assert choice_label("The Matrix", 1999, ["Lana Wachowski", "Lilly Wachowski", "X"]) == \
        "The Matrix (1999) — Lana Wachowski, Lilly Wachowski"
    assert choice_label("Untitled", None, None) == "Untitled"
    assert len(choice_label("A" * 200, 2000, ["B"])) == 100


def test_movie_from_api():
    m = Movie.from_api({"id": 603, "title": "The Matrix", "release_date": "1999-03-30",
                        "runtime": 136, "overview": "x", "poster_path": "/p.jpg",
                        "genres": [{"name": "Action"}],
                        "credits": {"crew": [{"job": "Director", "name": "Lana Wachowski"},
                                             {"job": "Writer", "name": "Someone"}]}})
    assert m.display == "The Matrix (1999)"
    assert m.runtime_text == "2h 16m"
    assert m.directors == ["Lana Wachowski"]
    assert m.letterboxd_url == "https://letterboxd.com/tmdb/603"
    assert Movie.from_api({"id": 1, "title": "T", "release_date": ""}).display == "T"
