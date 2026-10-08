import os

from conftest import file_item


def _sections():
    return [
        {
            "name": "General",
            "modules": [
                {"id": 10, "name": "Module Handbook", "modname": "resource", "uservisible": True,
                 "contents": [file_item("Handbook.pdf", b"H" * 100, 1000, cmid=10)]},
                {"id": 11, "name": "Announcements", "modname": "forum", "contents": []},
            ],
        },
        {
            "name": "Week 1: Intro/Basics",
            "modules": [
                {"id": 20, "name": "Lecture 1", "modname": "resource",
                 "contents": [file_item("Lecture 1.pptx", b"P" * 200, 2000, cmid=20)]},
                {"id": 21, "name": "Workshop materials", "modname": "folder",
                 "contents": [
                     file_item("Sheet.docx", b"D" * 50, 2100, cmid=21),
                     file_item("Data.csv", b"C" * 30, 2100, path="/data/", cmid=21),
                 ]},
                {"id": 22, "name": "Hidden", "modname": "resource", "uservisible": False,
                 "contents": [file_item("Secret.pdf", b"S", 1, cmid=22)]},
                {"id": 23, "name": "Reading", "modname": "page",
                 "contents": [file_item("index.html", b"<p>x</p>", 1, cmid=23)]},
            ],
        },
    ]


def _course():
    from unnc_moodle_mcp.api import Course

    return Course(id=7, fullname="Foundation Physics (DSEEF004 UNNC) (FCH1 26-27)", shortname="DSEEF004",
                  category="", startdate=0, enddate=0, url="")


def test_plan_files_layout_and_filters(fake):
    from unnc_moodle_mcp.sync import plan_files

    fake.set_contents(_sections())
    files = plan_files(fake.responses["core_course_get_contents"])
    rels = sorted(f.rel.as_posix() for f in files)
    assert rels == [
        "00 General/Handbook.pdf",
        "01 Week 1_ Intro_Basics/Lecture 1.pptx",
        "01 Week 1_ Intro_Basics/Workshop materials/Sheet.docx",
        "01 Week 1_ Intro_Basics/Workshop materials/data/Data.csv",
    ]
    only = plan_files(fake.responses["core_course_get_contents"], exts={"pptx", "pdf"})
    assert {f.rel.name for f in only} == {"Handbook.pdf", "Lecture 1.pptx"}


async def test_sync_is_incremental(fake, moodle, downloads):
    from unnc_moodle_mcp.sync import MANIFEST, sync_course

    fake.set_contents(_sections())
    course = _course()

    preview = await sync_course(moodle, course, dry_run=True)
    assert len(preview.new) == 4 and not (preview.root / MANIFEST).exists()

    first = await sync_course(moodle, course)
    assert len(first.new) == 4 and not first.failed
    lecture = first.root / "01 Week 1_ Intro_Basics" / "Lecture 1.pptx"
    assert lecture.read_bytes() == b"P" * 200
    assert int(os.stat(lecture).st_mtime) == 2000

    moodle.clear_cache()
    second = await sync_course(moodle, course)
    assert (len(second.new), len(second.updated), second.unchanged) == (0, 0, 4)

    # 老师替换了课件：修改时间和大小都变了
    sections = _sections()
    sections[1]["modules"][0]["contents"] = [file_item("Lecture 1.pptx", b"Q" * 260, 3000, cmid=20)]
    fake.set_contents(sections)
    moodle.clear_cache()
    third = await sync_course(moodle, course)
    assert [f.rel.name for f in third.updated] == ["Lecture 1.pptx"] and not third.new
    assert lecture.read_bytes() == b"Q" * 260


async def test_sync_never_overwrites_user_files(fake, moodle, downloads):
    from unnc_moodle_mcp.sync import course_dir, sync_course

    fake.set_contents(_sections())
    course = _course()
    mine = course_dir(course) / "00 General" / "Handbook.pdf"
    mine.parent.mkdir(parents=True)
    mine.write_bytes(b"my annotated copy")

    report = await sync_course(moodle, course)
    assert mine.read_bytes() == b"my annotated copy"
    assert (mine.parent / "Handbook (Moodle).pdf").read_bytes() == b"H" * 100
    assert not report.failed


async def test_sync_adopts_identical_existing_file(fake, moodle, downloads):
    from unnc_moodle_mcp.sync import course_dir, load_manifest, sync_course

    fake.set_contents(_sections())
    course = _course()
    existing = course_dir(course) / "00 General" / "Handbook.pdf"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"H" * 100)

    report = await sync_course(moodle, course, exts={"pdf"})
    assert report.unchanged == 1 and not report.new
    assert any(e["path"] == "00 General/Handbook.pdf" for e in load_manifest(report.root).values())


async def test_failed_download_is_retried_next_time(fake, moodle, downloads):
    from unnc_moodle_mcp.sync import sync_course

    sections = _sections()
    fake.set_contents(sections)
    bad = "/webservice/pluginfile.php/1/mod_resource/content/20/Lecture 1.pptx"
    fake.fail_paths.add(bad)
    course = _course()
    first = await sync_course(moodle, course)
    assert len(first.failed) == 1 and len(first.new) == 3

    fake.fail_paths.clear()
    moodle.clear_cache()
    second = await sync_course(moodle, course)
    assert [f.rel.name for f in second.new] == ["Lecture 1.pptx"] and not second.failed


async def test_only_cmid_includes_page_attachments(fake, moodle, downloads):
    from unnc_moodle_mcp.sync import sync_course

    fake.set_contents(_sections())
    report = await sync_course(moodle, _course(), only_cmid=23)
    assert [p.name for p in report.local_paths()] == ["index.html"]


async def test_subsections_are_nested_once(fake, moodle, downloads):
    from unnc_moodle_mcp.sync import plan_files

    fake.set_contents([
        {"id": 1, "name": "Submission Dropbox", "modules": [
            {"id": 50, "name": "Report", "modname": "subsection", "customdata": '{"sectionid":"9"}'},
        ]},
        {"id": 9, "name": "Report", "component": "mod_subsection", "modules": [
            {"id": 51, "name": "Template", "modname": "resource",
             "contents": [file_item("Template.docx", b"T" * 5, 1, cmid=51)]},
        ]},
    ])
    files = plan_files(fake.responses["core_course_get_contents"])
    assert [f.rel.as_posix() for f in files] == ["00 Submission Dropbox/Report/Template.docx"]


async def test_current_courses_include_recently_ended(fake, moodle):
    import time

    now = int(time.time())

    def by_class(form):
        cls = form["classification"][0]
        return {"courses": {
            "inprogress": [{"id": 1, "fullname": "Maths", "shortname": "M", "startdate": now - 30 * 86400, "enddate": now + 90 * 86400}],
            "past": [
                {"id": 2, "fullname": "Physics", "shortname": "P", "startdate": now - 20 * 86400, "enddate": now - 3 * 86400},
                {"id": 3, "fullname": "Old", "shortname": "O", "startdate": now - 900 * 86400, "enddate": now - 700 * 86400},
            ],
        }.get(cls, [])}

    fake.responses["core_course_get_enrolled_courses_by_timeline_classification"] = by_class
    assert [c.id for c in await moodle.current_courses()] == [1, 2]


def test_module_course_detection():
    from unnc_moodle_mcp.api import Course

    def c(short):
        return Course(1, "x", short, "", 0, 0, "")

    assert c("DSEEF011-1-UNNC-AUC-2627").is_module
    assert c("CELEN069-1-UNNC-AUC-2627").is_module
    assert not c("NONE-ASO-UNNC").is_module
    assert not c("FOSE-PRELIMINARYY-UNNC-2627").is_module
    assert not c("CELE-CELECS-UNNC-2627").is_module
