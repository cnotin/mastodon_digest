import argparse
import os
import sys
import tempfile
import webbrowser
from datetime import datetime, timedelta, timezone

from mastodon import Mastodon
from scipy import stats

from models import ScoredPost
from scorers import Scorer, get_scorers
from thresholds import get_thresholds


def fetch_posts_and_boosts(
    hours: int, mastodon_client: Mastodon, mastodon_username: str
) -> tuple[list[ScoredPost], list[ScoredPost]]:
    TIMELINE_LIMIT = 1000

    # First, get our filters
    filters = mastodon_client.filters()

    # Set our start query
    start = datetime.now(timezone.utc) - timedelta(hours=hours)

    posts = []
    boosts = []
    seen_post_urls = set()
    total_posts_seen = 0

    # Iterate over our home timeline until we run out of posts or we hit the limit
    response = mastodon_client.timeline(min_id=start)
    while response and total_posts_seen < TIMELINE_LIMIT:

        # Apply our server-side filters
        filtered_response = mastodon_client.filters_apply(response, filters, "home")

        for post in filtered_response:
            total_posts_seen += 1

            boost = False
            if post["reblog"] is not None:
                post = post["reblog"]  # look at the bosted post
                boost = True

            scored_post = ScoredPost(post)  # wrap the post data as a ScoredPost

            if scored_post.url not in seen_post_urls:
                # Apply our local filters
                # Basically ignore my posts or posts I've interacted with
                if (
                    not scored_post.info["reblogged"]
                    and not scored_post.info["favourited"]
                    and not scored_post.info["bookmarked"]
                    and scored_post.info["account"]["acct"] != mastodon_username
                ):
                    # Append to either the boosts list or the posts lists
                    if boost:
                        boosts.append(scored_post)
                    else:
                        posts.append(scored_post)
                    seen_post_urls.add(scored_post.url)

        response = mastodon_client.fetch_previous(
            response
        )  # fext the previous (because of reverse chron) page of results

    return posts, boosts


def run(
    hours: int,
    scorer: Scorer,
    threshold: int,
    mastodon_token: str,
    mastodon_base_url: str,
    mastodon_username: str,
) -> None:

    print(f"Building digest from the past {hours} hours...")

    mst = Mastodon(
        access_token=mastodon_token,
        api_base_url=mastodon_base_url,
    )

    posts, boosts = fetch_posts_and_boosts(hours, mst, mastodon_username)
    all_post_scores = [p.get_score(scorer) for p in posts]
    all_boost_scores = [b.get_score(scorer) for b in boosts]
    threshold_posts = [
        p
        for p in posts
        if stats.percentileofscore(all_post_scores, p.get_score(scorer)) > threshold
    ]
    threshold_boosts = [
        p
        for p in boosts
        if stats.percentileofscore(all_boost_scores, p.get_score(scorer)) > threshold
    ]

    # todo - do all this nonsense in Jinja or something better
    html_open = "<!DOCTYPE html>" "<html>"
    head = (
        "<head>"
        '<script src="https://static-cdn.mastodon.social/embed.js" async="async"></script>'
        "</head>"
    )
    body_open = '<body bgcolor="#292c36" style="font-family: Arial, sans-serif;">'
    container_open = '<div id="container" style="margin: auto; max-width: 640px; padding: 10px; text-align: center;">'
    title = '<h1 style="color:white;">Mastodon Digest</h1>'
    subtitle = f'<h3 style="color:#D3D3D3;"><i>Sourced from your timeline over the past {hours} hours</i></h2>'
    posts_header = (
        '<h2 style="color:white;">Here are some popular posts you may have missed:</h2>'
    )
    boosts_header = '<h2 style="color:white;">Here are some popular boosts you may have missed:</h2>'
    container_close = "</div>"
    body_close = "</body>"
    html_close = "</html>"

    content_collection = [
        [threshold_posts, ""],
        [threshold_boosts, ""],
    ]

    # print("Selecting posts...")
    for content in content_collection:
        for post in content[0]:
            content[1] += (
                '<div class="post">'
                f'<a style="color:white;" href=\'{post.get_home_url(mastodon_base_url)}\' target="_blank">Home Link</a>'
                '<span style="color:white;"> | </span>'
                f'<a style="color:white;" href=\'{post.url}\' target="_blank">Original Link</a>'
                "<br />"
                f'<iframe src=\'{post.url}/embed\' class="mastodon-embed" style="max-width: 100%; border: 0" width="400" allowfullscreen="allowfullscreen"></iframe>'
                "<br /><br />"
                "</div>"
            )

    output_html = (
        f"{html_open}"
        f"{head}"
        f"{body_open}"
        f"{container_open}"
        f"{title}"
        f"{subtitle}"
        f"{posts_header}"
        f"{content_collection[0][1]}"  # posts
        f"{boosts_header}"
        f"{content_collection[1][1]}"  # boosts
        f"{container_close}"
        f"{body_close}"
        f"{html_close}"
    )

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".html") as out_file:
        final_url = f"file://{out_file.name}"
        out_file.write(output_html)

    webbrowser.open(final_url)


if __name__ == "__main__":
    scorers = get_scorers()
    thresholds = get_thresholds()

    arg_parser = argparse.ArgumentParser(prog="mastodon_digest")
    arg_parser.add_argument(
        "-n",
        choices=range(1, 25),
        default=12,
        dest="hours",
        help="The number of hours to include in the Mastodon Digest",
        type=int,
    )
    arg_parser.add_argument(
        "-s",
        choices=list(scorers.keys()),
        default="SimpleWeighted",
        dest="scorer",
        help="""Which post scoring criteria to use. 
            SimpleWeighted is the default. 
            Simple scorers take a geometric mean of boosts and favs. 
            Extended scorers include reply counts in the geometric mean. 
            Weighted scorers multiply the score by an inverse sqaure root 
            of the author's followers, to reduce the influence of large accounts.
        """,
    )
    arg_parser.add_argument(
        "-t",
        choices=list(thresholds.keys()),
        default="normal",
        dest="threshold",
        help="""Which post threshold criteria to use. 
            Normal is the default.
            lax = 90th percentile
            normal = 95th percentile
            strict = 98th percentile
        """,
    )
    args = arg_parser.parse_args()
    if not args.hours:
        arg_parser.print_help()
    else:
        mastodon_token = os.getenv("MASTODON_TOKEN")
        mastodon_base_url = os.getenv("MASTODON_BASE_URL")
        mastodon_username = os.getenv("MASTODON_USERNAME")

        if not mastodon_token:
            sys.exit("Missing environment variable: MASTODON_TOKEN")
        if not mastodon_base_url:
            sys.exit("Missing environment variable: MASTODON_BASE_URL")
        if not mastodon_username:
            sys.exit("Missing environment variable: MASTODON_USERNAME")

        run(
            args.hours,
            scorers[args.scorer](),
            thresholds[args.threshold],
            mastodon_token,
            mastodon_base_url,
            mastodon_username,
        )
