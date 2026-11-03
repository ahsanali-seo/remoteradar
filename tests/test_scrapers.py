"""Each scraper's parser against saved sample pages (no network, no browser)."""
from radar.filters import free_filter
from radar.gmail import parse_alert
from radar.guard import detect_block
from radar.scrapers import boards, facebook, instagram, linkedin

from .conftest import fixture


def test_linkedin_job_list_and_view():
    jobs = linkedin.parse_job_list(fixture("linkedin_jobs.html"))
    assert [j["id"] for j in jobs] == ["4011111111", "4022222222"]
    assert jobs[0]["title"] == "SEO Specialist" and jobs[0]["company"] == "Pixel Orbit Digital"
    job = linkedin.parse_job_view(fixture("linkedin_job_view.html"), "4011111111", jobs[0])
    assert job.url == "https://www.linkedin.com/jobs/view/4011111111"
    assert job.company == "Pixel Orbit Digital" and "careers@pixelorbit.pk" in job.text
    assert job.posted_at is not None and job.remote_hint
    assert free_filter(job)[0]


def test_linkedin_posts():
    posts = linkedin.parse_posts(fixture("linkedin_posts.html"))
    assert len(posts) == 2
    assert posts[0].author == "Sana Malik"
    assert posts[0].url.endswith("urn:li:activity:7100000000000000001/")
    assert free_filter(posts[0])[0]


def test_facebook_group_skips_comments():
    posts = facebook.parse_group(fixture("facebook_group.html"), "https://www.facebook.com/groups/lahorejobs")
    assert len(posts) == 2
    p = posts[0]
    assert "Local SEO Specialist" in p.text and "comment" not in p.text
    assert p.url == "https://www.facebook.com/groups/lahorejobs/posts/123456789/"
    assert p.posted_at is not None
    assert free_filter(p)[0] and not free_filter(posts[1])[0]
    assert facebook.group_url_newest("https://www.facebook.com/groups/x/?ref=share") == \
        "https://www.facebook.com/groups/x/?sorting_setting=CHRONOLOGICAL"


def test_instagram(tmp_home):
    links = instagram.parse_grid(fixture("instagram_grid.html"))
    assert links == ["https://www.instagram.com/p/Cx1AAA/", "https://www.instagram.com/p/Cx2BBB/",
                     "https://www.instagram.com/reel/Cx3CCC/"]
    post = instagram.parse_post(fixture("instagram_post.html"), links[0])
    assert "Remote SEO Manager" in post.text and post.author == "Growthly Agency"
    assert post.posted_at.year == 2026
    (tmp_home / "sources" / "instagram.txt").write_text("# comment line\n#seojobs\n@growthly\n")
    assert instagram.read_targets() == ["#seojobs", "@growthly"]
    assert instagram.target_url("#seojobs") == "https://www.instagram.com/explore/tags/seojobs/"


def test_indeed_and_rozee_listings():
    ind = boards.harvest_links(fixture("indeed.html"), "https://pk.indeed.com/jobs?q=SEO",
                               r"/viewjob|/rc/clk|jk=")
    assert len(ind) == 1 and ind[0]["url"].startswith("https://pk.indeed.com/rc/clk?jk=abc123")
    assert "Nexa Marketing" in ind[0]["card"]
    roz = boards.harvest_links(fixture("rozee.html"), "https://www.rozee.pk/job/jsearch/q/seo",
                               r"rozee\.pk/.+-jobs?-\d+|/job/")
    assert len(roz) == 1 and roz[0]["url"] == "https://www.rozee.pk/softmind-seo-specialist-lahore-jobs-1234567"
    job = boards.parse_detail(fixture("board_detail.html"), roz[0]["url"], "rozee", roz[0], "Rozee.pk")
    assert job.title == "SEO Specialist" and job.company == "Softmind" and job.location == "Lahore"
    assert "nav stuff" not in job.text and job.posted_at is not None
    assert free_filter(job)[0]


def test_job_boards_csv(tmp_home):
    (tmp_home / "sources" / "job_boards.csv").write_text(
        "platform,name,url,link_pattern\n"
        "indeed,Indeed Lahore,https://pk.indeed.com/jobs?q=SEO&l=Lahore,jk=\n"
        "indeed,Indeed Islamabad,https://pk.indeed.com/jobs?q=SEO&l=Islamabad,jk=\n"
        "rozee,Mustakbil,https://www.mustakbil.com/jobs/search?keywords=seo,/jobs/\n")
    b = boards.load_boards()
    assert len(b["Indeed"]["urls"]) == 2 and b["Mustakbil"]["platform"] == "rozee"


def test_careers_page():
    jobs = boards.parse_careers(fixture("careers.html"),
                                {"company": "Brandverse", "city": "Islamabad",
                                 "careers_url": "https://brandverse.pk/careers", "website": "brandverse.pk"})
    assert len(jobs) == 1
    assert jobs[0].url == "https://brandverse.pk/careers/technical-seo-lead"
    assert free_filter(jobs[0])[0]


def test_gmail_alert():
    jobs = parse_alert(fixture("gmail_alert.html"), "", "LinkedIn <jobalerts-noreply@linkedin.com>")
    assert len(jobs) == 1 and "Clickforge" in jobs[0].text
    assert free_filter(jobs[0])[0]


def test_block_detection():
    assert detect_block("https://www.linkedin.com/checkpoint/challenge/x", "")
    assert detect_block("https://www.linkedin.com/feed/", fixture("blocked_linkedin.html"))
    assert detect_block("https://www.instagram.com/accounts/login/?next=/explore/", "")
    assert detect_block("https://x.com", "", has_block_element=True)
    assert detect_block("https://www.linkedin.com/jobs/search/?keywords=SEO", "SEO Specialist jobs") is None
