import asyncio
import csv
import os
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention

import re
import io
import random
from urllib.parse import urlparse

import aiofiles
import aiohttp
from bs4 import BeautifulSoup
from tqdm.asyncio import tqdm_asyncio 


async def fetch_sitemap(session,url,headers):
    """Bounded bootstrap retries; failed downloads never select cached data."""
    for attempt in range(3):
        try:
            async with session.get(url,headers=headers,timeout=aiohttp.ClientTimeout(total=90)) as response:
                if response.status in {401,403,429}:raise PermissionError('Wells Fargo sitemap denied access; no retry')
                response.raise_for_status()
                body=await response.text()
                if not body.strip():raise ValueError('Empty Wells Fargo sitemap')
                return body
        except (aiohttp.ClientError,asyncio.TimeoutError) as exc:
            if isinstance(exc,aiohttp.ClientResponseError) and exc.status not in {408,425,429,500,502,503,504}:
                raise
            if attempt==2:raise
            print('Sitemap bootstrap retry '+str(attempt+1)+'/2: '+type(exc).__name__,flush=True)
            await asyncio.sleep(2**(attempt+1))

# Define output path: data/raw/firm_rosters/wells_fargo_advisors.csv
# Scraped firm rosters are INPUTS, not pipeline output: they live in
# data/raw/firm_rosters/ so a rebuild of data/output/ cannot overwrite them.
OUTPUT_DIR = os.path.join("data", "raw", "firm_rosters")
OUTPUT_FILE = str(roster_path("wells_fargo"))

FIELDNAMES = ["url", "name", "title", "emails", "phone_numbers", "address", "team_name", "team_url",
              "record_type", "name_source", "raw_heading", "name_review_reason"]


def _contact_link(card, scheme):
    """Use a link from this person's card, never another team member."""
    if scheme == 'tel':
        links=[a for a in card.select('.card--contact-info a[href]') if a.get('href','').lower().startswith('tel:')]
        if not links and 'biz--card' in card.get('class',[]) and len(card.select('h2.name'))==1:
            links=[a for a in card.select('.container--contact a[href]') if a.get('href','').lower().startswith('tel:')]
        phones={}
        for link in links:
            href=link.get('href','')
            if not href.lower().startswith('tel:'):continue
            if re.search(r'\bfax\b',link.parent.get_text(' ',strip=True) if link.parent else '',re.I):continue
            value=href.split(':',1)[-1].split('?',1)[0].strip()
            digits=re.sub(r'\D','',value)
            if len(digits)<10:continue
            key=digits[1:] if len(digits)==11 and digits.startswith('1') else digits
            phones.setdefault(key,value)
        return '; '.join(phones.values())
    link = card.select_one(f'.card--contact-info a[href^="{scheme}:"]')
    if not link and 'biz--card' in card.get('class',[]) and len(card.select('h2.name'))==1:
        link=card.select_one(f'.container--contact a[href^="{scheme}:"]')
    if not link:
        return ""
    value = link.get("href", "").split(":", 1)[-1].split("?", 1)[0].strip()
    return value if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) else ""


def _published_person_name(value):
    """Separate source-local credentials and role footnotes, not legal suffixes."""
    value = value.strip().lstrip('* ').replace('\u00ae', '').replace('\u2122', '')
    value = re.split(r'\s+CA\s+Insurance\s+License\s+Number\b', value, maxsplit=1, flags=re.I)[0]
    value = re.split(r'\*|\b\d{3}[-.) ]+\d{3}[-. ]+\d{4}\b', value, maxsplit=1)[0].strip(' ,')
    credentials = r'(?:CFP|CFA|CPWA|CRPC|CRPS|CSRIC|AAMS|SE-AWMA|AWMA|RICP|CRC|CMFC|CDFA|MBA|JD|ESQ|CLU|CHFC|CPA|CIMA|CPFA|AIF)'
    value = re.split(r'(?:,\s*|\s+)' + credentials + r'\b', value, maxsplit=1, flags=re.I)[0]
    return value.strip(' ,')


def _person_label(value):
    value=_published_person_name(value)
    words=re.findall(r"[^\W\d_]+",value.lower(),re.UNICODE)
    business={'group','advisors','advisor','wealth','management','financial','planning','relationships',
        'your','our','we','who','with','welcome','home','services','solutions','retirement','the',
        'should','consider','you','us','about','team','trust','goals','strategy','serving','expertise',
        'business','owners','executives','resources','sophisticated','approach','client',
        'investment','comprehensive','risk','partners','associates','proven','experience'}
    return value if 2<=len(words)<=6 and not business.intersection(words) and not re.search(r'[!?;:&]|\band\b',value,re.I) else ''


def _name_words(name):
    name=_published_person_name(name).lower().replace("'",'').replace('\u2019','')
    words=re.findall(r"[^\W\d_]+",name,re.UNICODE)
    if words and words[-1] in {'jr','sr','ii','iii','iv','v'}:words=words[:-1]
    if len(words)>=3 and words[-2] in {'al','van','de','del','da','la','von','di','dos','des','san'}:
        words=words[:-2]+[''.join(words[-2:])]
    return words


def _mail_agrees(name,address):
    words=_name_words(name)
    if len(words)<2:return False
    local=address.split('@',1)[0].lower()
    tokens=re.findall('[a-z]+',local)
    compact=''.join(tokens)
    last=words[-1]
    surname_parts=re.findall(r"[^\W\d_]+",_published_person_name(name).split()[-1].lower(),re.UNICODE) if '-' in _published_person_name(name).split()[-1] else []
    surnames={last,''.join(surname_parts),*surname_parts}-{''}
    # Initial-first and explicit parenthetical/middle given names are common.
    # Never invent a nickname or infer a surname change here.
    givens=words[:-1]
    return (len(tokens)>=2 and tokens[-1] in surnames and any(tokens[0].startswith(g[:1]) for g in givens)
            or any(compact in {g+surname,g[:1]+surname} for g in givens for surname in surnames))


def _heading_agrees(name,emails):
    # A marketing h1 is not an identity assertion. Require a personal-mailbox
    # corroboration; a unique first-name-only address is allowed on a named
    # individual page, but not an office/info mailbox.
    cleaned=_published_person_name(name)
    given=_name_words(cleaned)
    if not given:return False
    # Published individual pages also use surname + first/middle initials
    # (John Woods -> woodsja). This corroborates a source heading only; it
    # never generates an address or establishes a CRD/person link.
    surname_first=any(re.fullmatch(re.escape(given[-1]+given[0][0])+r'[a-z]?',e.split('@',1)[0].lower())
        for e in emails) if len(given)>=2 else False
    return any(_mail_agrees(cleaned,email) for email in emails) or (
        len(emails)==1 and emails[0].split('@',1)[0].lower()==given[0]) or surname_first


def _profile_heading_agrees(name,url):
    """Allow named profiles when their published URL corroborates the heading.

    This recognizes the name, not ownership of an unrelated office mailbox.
    A surname alone or a marketing slogan in a URL cannot satisfy this rule.
    """
    cleaned=_published_person_name(name)
    if not _person_label(cleaned):return False
    words=_name_words(cleaned)
    slug_words=re.findall(r"[^\W\d_]+",urlparse(url).path.strip('/').split('/')[0].lower(),re.UNICODE)
    return len(words)>=2 and words[-1] in slug_words and any(w in slug_words for w in words[:-1] if len(w)>1)


def _personal_bio(card,name):
    """Bare staff cards need person evidence; marketing cards reuse this CSS."""
    bio=card.select_one('[id$="-bio"]')
    text=bio.get_text(' ',strip=True)[:600].lower() if bio else ''
    words=_name_words(name)
    if len(words)<2:return False
    if words[-1] in {'jr','sr','ii','iii','iv','v'}:words=words[:-1]
    if len(words)<2:return False
    first,last=words[0],words[-1]
    portrait=card.select_one('img.headshot[src]')
    filename=portrait.get('src','').lower() if portrait else ''
    compact=re.sub('[^a-z]','',filename.rsplit('/',1)[-1])
    if len(first)>1 and len(last)>2 and first+last in compact:return True
    if not re.search(r'\b'+re.escape(first)+r'\b',text):return False
    if re.search(r'\b'+re.escape(last)+r'\b',text):return True
    # A portrait filename can corroborate a surname typo in the biography.
    return len(last)>=4 and last in filename


def related_team_urls(html,url):
    from urllib.parse import urljoin
    source=urlparse(url)
    base=source.path.rsplit('/',1)[0]+'/' if '.' in source.path.rsplit('/',1)[-1] else source.path.rstrip('/')+'/'
    found=set()
    for link in BeautifulSoup(html,'html.parser').select('a[href]'):
        label=' '.join(link.get_text(' ',strip=True).lower().split())
        target=urljoin(url,link['href']);parsed=urlparse(target)
        team_label=label in {'our team','meet our team','meet the team','our people','our associates','team','about us'}
        if (team_label and parsed.scheme=='https' and parsed.hostname=='fa.wellsfargoadvisors.com'
                and parsed.hostname==source.hostname and parsed.path.startswith(base)
                and parsed.path!=source.path and not parsed.query and not parsed.fragment):
            found.add(target)
    return sorted(found)[:2]


def parse_page(html,url):
    import json
    soup=BeautifulSoup(html,'html.parser')
    organization={}
    for script in soup.select('script[type="application/ld+json"]'):
        try:payload=json.loads(script.get_text())
        except (ValueError,TypeError):continue
        items=payload if isinstance(payload,list) else payload.get('@graph',[payload]) if isinstance(payload,dict) else []
        for item in items:
            if isinstance(item,dict) and item.get('@type') in ('LocalBusiness','FinancialService'):
                organization=item;break
    published_address=organization.get('address')
    if isinstance(published_address,dict):
        published_address=' '.join(str(published_address.get(k) or '') for k in
            ('streetAddress','addressLocality','addressRegion','postalCode')).strip()
    address_elem=soup.select_one('a[href*="maps"], .address, [class*="address"]')
    address=(published_address if isinstance(published_address,str) and published_address.strip() else
             ' '.join(address_elem.get_text(' ',strip=True).split()) if address_elem else '')
    if not address:
        match=re.search(r'\d+\s+[\w\s]+(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way)\.?,?\s+[\w\s]+,\s+[A-Z]{2}\s+\d{5}',soup.get_text())
        address=match.group(0) if match else ''
    title_text=soup.title.get_text(' ',strip=True) if soup.title else ''
    title_parts=[x.strip() for x in title_text.split('|')]
    team_name=str(organization.get('name') or (title_parts[1] if len(title_parts)>1 else title_parts[0])).split(',',1)[0].strip()
    # Wells publishes a second, official single-person business-card layout.
    # Scope its contacts to that card, not the page footer or a colleague.
    cards=[c for c in soup.select('.section--team .card--profile,.section--team .biz--card')
        if 'biz--card' not in c.get('class',[]) or len(c.select('h2.name'))==1]
    card_ids={id(c) for c in cards}
    rows=[]
    for card in cards:
        element=card.select_one('.container--card-title h2.name .name-data')
        if not element and 'biz--card' in card.get('class',[]):element=card.select_one('h2.name > .name-data')
        if not element:continue
        # Some custom cards append a job description after a line break inside
        # name-data. Preserve the published name, not the concatenated role.
        first_line=re.split(r'<br\b[^>]*>',str(element),maxsplit=1,flags=re.I)[0]
        name=BeautifulSoup(first_line,'html.parser').get_text(' ',strip=True).strip(' ,')
        raw_name=name
        name=_published_person_name(name)
        if not name:continue
        job=card.select_one('.container--card-title > p.title')
        if not job and 'biz--card' in card.get('class',[]):
            job=element.find_parent('h2').parent.select_one('p.title')
        has_contact=bool(_contact_link(card,'mailto') or _contact_link(card,'tel'))
        has_person_context=has_contact or bool(job and job.get_text(strip=True)) or raw_name!=name or _personal_bio(card,name)
        if not _person_label(name) or not has_person_context:
            # Topic/content cards can reuse the profile CSS. No personal
            # contact assertion exists when the label is not a person name.
            if not has_contact:continue
            rows.append({'url':url,'name':'','title':'','emails':_contact_link(card,'mailto'),
                'phone_numbers':_contact_link(card,'tel'),'address':address,'team_name':team_name,'team_url':url,
                'record_type':'office_or_unresolved','name_source':'','raw_heading':name,
                'name_review_reason':'non_person_label_on_profile_card'})
            continue
        card_address=card.select_one('[itemprop="address"],.address,.card--address')
        rows.append({'url':url,'name':name,'title':job.get_text(' ',strip=True) if job else '',
            'emails':_contact_link(card,'mailto'),'phone_numbers':_contact_link(card,'tel'),
            'address':card_address.get_text(' ',strip=True) if card_address else address,
            'team_name':team_name,'team_url':url,'record_type':'person',
            'name_source':'person_card','raw_heading':raw_name if raw_name!=name else '', 'name_review_reason':''})
    # Outside-card links may be the page owner or the office. Never append a
    # colleague's card emails/phones to the owner of an individual profile.
    unscoped=[link for link in soup.select('a[href]') if not any(id(p) in card_ids for p in link.parents)]
    emails=sorted({a['href'][7:].split('?',1)[0].strip().lower() for a in unscoped if a['href'].lower().startswith('mailto:')})
    phones=sorted({a['href'][4:].split('?',1)[0].strip() for a in unscoped if a['href'].lower().startswith('tel:')
        and 'fax' not in (a.parent.get_text(' ',strip=True) if a.parent else '').lower()})
    candidate=_person_label(re.split(r'\s[-|]\s|,',title_text,maxsplit=1)[0].strip())
    headings=[h for h in soup.select('h2.name') if not any(id(p) in card_ids for p in h.parents)]
    heading=soup.select_one('div.page--title h1') or soup.find('h1')
    raw_heading=heading.get_text(' ',strip=True) if heading else ''
    name='';name_source=''
    if candidate and any(_mail_agrees(candidate,e) for e in emails):name=candidate;name_source='corroborated_document_title'
    elif len(headings)==1 and _person_label(headings[0].get_text(' ',strip=True)):
        name=_person_label(headings[0].get_text(' ',strip=True));name_source='person_heading'
    elif not cards and _person_label(raw_heading) and (_heading_agrees(raw_heading,emails) or _profile_heading_agrees(raw_heading,url)):
        name=_published_person_name(raw_heading);name_source='person_page_heading'
    if name:
        agreed=[e for e in emails if _mail_agrees(name,e)]
        owner_emails=agreed or (emails if len(emails)==1 else [])
        if name_source=='person_page_heading':
            owner_emails=[e for e in emails if _heading_agrees(name,[e])]
        job=soup.select_one('div.page--title p.title')
        owner={'url':url,'name':name,'title':job.get_text(' ',strip=True) if job else '',
            'emails':'; '.join(owner_emails),'phone_numbers':'; '.join(phones),'address':address,
            'team_name':'','team_url':'','record_type':'person','name_source':name_source,
            'raw_heading':raw_heading,'name_review_reason':''}
        if not any(r['name'].casefold()==name.casefold() and r['emails'].casefold()==owner['emails'].casefold() for r in rows):rows.insert(0,owner)
        emails=[e for e in emails if e not in owner_emails]
    represented={e.strip().lower() for row in rows for e in row['emails'].split(';') if e.strip()}
    remaining=[e for e in emails if e.lower() not in represented]
    if not rows or remaining:
        rows.append({'url':url,'name':'','title':'','emails':'; '.join(remaining),
            'phone_numbers':'; '.join(phones),'address':address,'team_name':team_name,'team_url':url,
            'record_type':'office_or_unresolved','name_source':'','raw_heading':raw_heading,
            'name_review_reason':'no_person_scoped_name_for_published_contacts'})
    return rows


class SourceEvidenceError(RuntimeError):
    """A source cannot be accepted without its promised retained evidence."""


def retain_page_observation(journal,url,final_url,status,html):
    try:return _retain_page_observation(journal,url,final_url,status,html)
    except OSError as exc:raise SourceEvidenceError('Wells source evidence could not be retained') from exc


def _retain_page_observation(journal,url,final_url,status,html):
    """Retain decoded source markup so parser reviews need no repeat crawl."""
    import gzip,hashlib,json
    from refresh_core import utc
    journal=_pathlib.Path(journal)
    body=html.encode('utf-8');digest=hashlib.sha256(body).hexdigest()
    folder=journal.with_suffix('');folder.mkdir(parents=True,exist_ok=True)
    target=folder/(digest+'.html.gz')
    if not target.exists():target.write_bytes(gzip.compress(body,mtime=0))
    item={'url':url,'finalUrl':final_url,'status':status,'observedUtc':utc(),
        'decodedHtmlSha256':digest,'decodedHtmlBytes':len(body),'htmlFile':str(target)}
    with journal.open('a',encoding='utf-8') as f:f.write(json.dumps(item,sort_keys=True)+'\n')


async def parse_advisor_page(session, url, capture=None):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    }

    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as response:
            if capture and response.status!=200:capture(url,str(response.url),response.status,'')
            if response.status in {401,403,429}:
                raise PermissionError('Wells Fargo page denied access; no retry')
            if response.status != 200:
                # We will log the error but let the retry logic handle it
                tqdm_asyncio.write(f"HTTP {response.status} Error scraping {url}")
                return None
            
            html = await response.text()
            if capture:capture(url,str(response.url),200,html)
            if not html.strip() or urlparse(str(response.url)).hostname!='fa.wellsfargoadvisors.com':return None

        rows=parse_page(html,url)
        for related in related_team_urls(html,url):
            async with session.get(related,headers=headers,timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status in {401,403,429}:
                    if capture:capture(related,str(response.url),response.status,'')
                    raise PermissionError('Wells Fargo team page denied access; no retry')
                if response.status!=200:return None
                if urlparse(str(response.url)).hostname!='fa.wellsfargoadvisors.com':return None
                related_html=await response.text()
                if capture:capture(related,str(response.url),200,related_html)
                if not related_html.strip():return None
                extra=parse_page(related_html,related)
                rows.extend(row for row in extra if row.get('record_type')=='person')
        seen=set();output=[]
        for row in rows:
            key=(row['name'].casefold(),row['emails'].casefold(),row['phone_numbers'],row['address'])
            if key not in seen:seen.add(key);output.append(row)
        return output
    except (PermissionError,SourceEvidenceError):
        raise
    except Exception as e:
        tqdm_asyncio.write(f"Exception scraping {url}: {e}")
        return None


async def process_sitemap(sitemap_url, max_concurrent=25, max_retries=5):
    # Ensure directory exists
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    partial_file = OUTPUT_FILE + ".partial"
    write_lock=asyncio.Lock()
    journal=scratch_path('wells_fargo','page_observations',ext='jsonl')
    journal.parent.mkdir(parents=True,exist_ok=True)
    journal.touch(exist_ok=False)
    def capture(url,final_url,status,html):retain_page_observation(journal,url,final_url,status,html)

    async with aiohttp.ClientSession() as session:
        # Fetch sitemap and extract all <loc> URLs
        print("Fetching sitemap...")
        sitemap_xml = await fetch_sitemap(session,sitemap_url,headers)

        soup = BeautifulSoup(sitemap_xml, "xml")
        urls_to_scrape = list(dict.fromkeys(
            loc.get_text(strip=True) for loc in soup.find_all("loc")
            if loc.text and urlparse(loc.get_text(strip=True)).hostname == "fa.wellsfargoadvisors.com"
        ))
        if len(urls_to_scrape) < 1000:
            raise ValueError(f"Wells Fargo sitemap is unexpectedly small: {len(urls_to_scrape)} URLs")
        
        total_urls = len(urls_to_scrape)
        print(f"Found {total_urls} URLs to scrape.")

        # Prepare CSV File and write header once
        rows_written = 0
        team_rows = 0
        async with aiofiles.open(partial_file, mode="w", newline="", encoding="utf-8") as f:
            await f.write(",".join(FIELDNAMES) + "\n")

            # Loop through initial attempt + retries
            for attempt in range(max_retries + 1):
                if not urls_to_scrape:
                    print("\nAll URLs scraped successfully!")
                    break

                if attempt > 0:
                    print(f"\n[Attempt {attempt + 1}/{max_retries + 1}] Retrying {len(urls_to_scrape)} failed URLs.")
                    print("Waiting 10 seconds before restarting...")
                    await asyncio.sleep(10)
                else:
                    print(f"\n[Attempt 1/{max_retries + 1}] Starting initial scrape...")

                semaphore = asyncio.Semaphore(max_concurrent)
                denied=asyncio.Event()

                async def worker(url):
                    nonlocal rows_written, team_rows
                    async with semaphore:
                        if denied.is_set():return url
                        await asyncio.sleep(random.uniform(0.1, 0.7))
                        if denied.is_set():return url
                        try:data = await parse_advisor_page(session, url,capture=capture)
                        except PermissionError:
                            denied.set()
                            raise
                        
                        if data is not None:
                            output = io.StringIO()
                            writer = csv.DictWriter(output, fieldnames=FIELDNAMES)
                            writer.writerows(data)
                            async with write_lock:
                                await f.write(output.getvalue())
                                rows_written += len(data)
                                team_rows += sum(bool(row["team_url"]) and row.get('record_type')=='person' for row in data)
                            return None
                        else:
                            # Failure: return the URL so we can try it again
                            return url

                # Run current batch of tasks
                tasks = [asyncio.create_task(worker(url)) for url in urls_to_scrape]
                results=[]
                try:
                    for task in tqdm_asyncio.as_completed(tasks,desc='Scraping'):results.append(await task)
                finally:
                    for task in tasks:
                        if not task.done():task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                
                # Filter results to only keep the URLs that failed (returned themselves instead of None)
                urls_to_scrape = [url for url in results if url is not None]

    # A tiny number of sitemap entries can become permanent redirect loops.
    # Record them for review, but do not discard an otherwise complete census.
    max_failed = max(1, int(total_urls * 0.001))
    if len(urls_to_scrape) > max_failed or rows_written < total_urls * 0.8:
        raise RuntimeError(
            f"Wells Fargo scrape incomplete: {len(urls_to_scrape)} failed URLs, "
            f"{rows_written} rows from {total_urls} sitemap URLs. "
            f"Partial file retained at {partial_file}; prior roster remains current."
        )
    failed_file = scratch_path("wells_fargo", "failed", ext="csv")
    with failed_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["url"])
        writer.writerows((url,) for url in urls_to_scrape)
    if urls_to_scrape:
        print(f"Warning: {len(urls_to_scrape)} unresolved URLs recorded in {failed_file}")
    os.replace(partial_file, OUTPUT_FILE)
    print(f"Results saved to {OUTPUT_FILE}: {rows_written} rows, {team_rows} team members")


if __name__ == "__main__":
    sitemap_url = "https://fa.wellsfargoadvisors.com/sitemap.xml"
    # Modest parallelism across the public sitemap; failed pages are retried.
    asyncio.run(process_sitemap(sitemap_url, max_concurrent=6, max_retries=5))
