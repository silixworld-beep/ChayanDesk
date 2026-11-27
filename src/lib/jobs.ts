import type { CollectionEntry } from 'astro:content';
import {
  AUTHORITY_META,
  SALARY_TIERS,
  SITE_CONFIG,
  type AuthorityType,
  type SalaryTierSlug,
} from '../config/site';

export type JobEntry = CollectionEntry<'jobs'>;
export type AdmissionEntry = CollectionEntry<'admissions'>;
export type GuideEntry = CollectionEntry<'guides'>;
export type GovernmentLevel = 'central' | 'state';

export const SITE_NAME = SITE_CONFIG.brandName;
export const SITE_TAGLINE = SITE_CONFIG.tagline;
export const FALLBACK_TEXT = 'Visit Official Site';

const MS_PER_DAY = 86_400_000;

const STATE_MATCHERS: Array<{ canonical: string; aliases: string[]; domains: string[] }> = [
  { canonical: 'Andhra Pradesh', aliases: ['andhra pradesh', 'appsc', 'amaravati', 'visakhapatnam', 'vijayawada', 'tirupati', 'guntur'], domains: ['.ap.gov.in', 'psc.ap.gov.in'] },
  { canonical: 'Arunachal Pradesh', aliases: ['arunachal pradesh', 'itanagar', 'appsc itanagar'], domains: ['arunachalpradesh.gov.in'] },
  { canonical: 'Assam', aliases: ['assam', 'apsc', 'dispur', 'guwahati', 'slprb assam'], domains: ['assam.gov.in', 'apsc.nic.in'] },
  { canonical: 'Bihar', aliases: ['bihar', 'bpsc', 'bssc', 'csbc', 'btsc', 'brlps', 'jeevika', 'patna', 'rohtas', 'gaya', 'muzaffarpur'], domains: ['.bihar.gov.in', 'brlps.in', 'rohtas.nic.in', 'bpsc.bih.nic.in'] },
  { canonical: 'Chhattisgarh', aliases: ['chhattisgarh', 'cgpsc', 'cgvyapam', 'csptcl', 'raipur', 'bilaspur cg'], domains: ['.cg.gov.in', 'cgstate.gov.in', 'cspdcl.co.in'] },
  { canonical: 'Delhi', aliases: ['delhi', 'dsssb', 'nct of delhi', 'lok nayak hospital', 'new delhi'], domains: ['delhi.gov.in', 'dsssb.delhi.gov.in'] },
  { canonical: 'Goa', aliases: ['goa', 'goapsc', 'panaji'], domains: ['goa.gov.in'] },
  { canonical: 'Gujarat', aliases: ['gujarat', 'gpsc', 'gsssb', 'lrb gujarat', 'gandhinagar', 'ahmedabad', 'surat', 'vadodara'], domains: ['.gujarat.gov.in', 'gujarat.gov.in', 'gsssb.gujarat.gov.in'] },
  { canonical: 'Haryana', aliases: ['haryana', 'hssc', 'hpsc', 'sonepat', 'sonipat', 'gurugram', 'faridabad', 'hisar', 'rohtak', 'panchkula', 'karnal'], domains: ['.haryana.gov.in', 'hssc.gov.in', 'cracollegesonepat.org'] },
  { canonical: 'Himachal Pradesh', aliases: ['himachal pradesh', 'hppsc', 'hprca', 'hpscb', 'shimla', 'parwanoo', 'solan', 'baddi', 'kangra', 'mandi'], domains: ['.hp.gov.in', 'hpscb.bank.in', 'parwanoourban.bank.in'] },
  { canonical: 'Jammu & Kashmir', aliases: ['jammu & kashmir', 'jammu and kashmir', 'jkssb', 'jkpsc', 'j&k bank', 'j k bank', 'jammu kashmir bank', 'srinagar', 'jammu'], domains: ['jkssb.nic.in', 'jkpsc.nic.in', 'jkb.bank.in', '.jk.gov.in'] },
  { canonical: 'Jharkhand', aliases: ['jharkhand', 'jpsc', 'jssc', 'jrhms', 'ranchi', 'jamshedpur', 'dhanbad', 'bokaro'], domains: ['.jharkhand.gov.in', 'jssc.nic.in'] },
  { canonical: 'Karnataka', aliases: ['karnataka', 'kpsc', 'kea', 'karunadu', 'mysuru', 'hubballi', 'mangaluru', 'belagavi'], domains: ['karnataka.gov.in', 'kpsc.kar.nic.in'] },
  { canonical: 'Kerala', aliases: ['kerala', 'kerala psc', 'c-dit', 'cmd kerala', 'thiruvananthapuram', 'kochi', 'kozhikode', 'thrissur'], domains: ['kerala.gov.in', 'cdit.org'] },
  { canonical: 'Madhya Pradesh', aliases: ['madhya pradesh', 'mppsc', 'mpesb', 'mp vyapam', 'mprdc', 'indore', 'jabalpur', 'gwalior'], domains: ['.mp.gov.in', 'mponline.gov.in'] },
  { canonical: 'Maharashtra', aliases: ['maharashtra', 'mpsc', 'mahapariksha', 'akola', 'vasai virar', 'vasai-virar', 'vvcmc', 'nagpur', 'pune', 'thane', 'nashik', 'aurangabad'], domains: ['maharashtra.gov.in', 'akola.gov.in', 'msc.bank.in', 'akolajanata.bank.in', 'vvcmc.in'] },
  { canonical: 'Manipur', aliases: ['manipur', 'imphal'], domains: ['manipur.gov.in'] },
  { canonical: 'Meghalaya', aliases: ['meghalaya', 'shillong'], domains: ['meghalaya.gov.in'] },
  { canonical: 'Mizoram', aliases: ['mizoram', 'aizawl'], domains: ['mizoram.gov.in'] },
  { canonical: 'Nagaland', aliases: ['nagaland', 'kohima'], domains: ['nagaland.gov.in'] },
  { canonical: 'Odisha', aliases: ['odisha', 'orissa', 'opsc', 'ossc', 'osssc', 'bhubaneswar', 'cuttack'], domains: ['odisha.gov.in', 'ossc.gov.in', 'osssc.gov.in'] },
  { canonical: 'Punjab', aliases: ['punjab', 'psssb', 'ppsc', 'patiala', 'ludhiana', 'amritsar', 'jalandhar', 'mohali'], domains: ['punjab.gov.in', 'psssb.punjab.gov.in', 'mbspsu.ac.in'] },
  { canonical: 'Rajasthan', aliases: ['rajasthan', 'rpsc', 'rssb', 'rsmssb', 'jaipur', 'jodhpur', 'bikaner', 'ajmer', 'kota'], domains: ['rajasthan.gov.in', 'rssb.rajasthan.gov.in'] },
  { canonical: 'Sikkim', aliases: ['sikkim', 'gangtok'], domains: ['sikkim.gov.in'] },
  { canonical: 'Tamil Nadu', aliases: ['tamil nadu', 'tnpsc', 'tnusrb', 'mrb tn', 'trb tn', 'nilgiris', 'chennaicorporation', 'coimbatore', 'madurai', 'salem', 'tiruchirappalli'], domains: ['tn.gov.in', 'chennaicorporation.gov.in', 'nilgiris.nic.in'] },
  { canonical: 'Telangana', aliases: ['telangana', 'tspsc', 'tgpsc', 'rajanna sircilla', 'sircilla', 'warangal', 'karimnagar', 'nizamabad'], domains: ['telangana.gov.in', 'gmcrajannasircilla.org'] },
  { canonical: 'Tripura', aliases: ['tripura', 'agartala'], domains: ['tripura.gov.in'] },
  { canonical: 'Uttar Pradesh', aliases: ['uttar pradesh', 'uppsc', 'upsssc', 'uppbpb', 'uppcl', 'mirzapur', 'lucknow', 'varanasi', 'prayagraj', 'kanpur', 'agra', 'meerut', 'noida', 'ghaziabad', 'ballia', 'hardoi', 'pilibhit', 'maharajganj', 'bhadohi', 'banda', 'budaun', 'sant kabir nagar', 'lalitpur', 'gorakhpur'], domains: ['.up.gov.in', 'uppsc.up.nic.in', 'upsssc.gov.in', 'upanganwadibharti.in', 'up.nic.in'] },
  { canonical: 'Uttarakhand', aliases: ['uttarakhand', 'ukpsc', 'uksssc', 'dehradun', 'haldwani', 'haridwar', 'nainital'], domains: ['.uk.gov.in', 'ukpsc.net.in', 'psc.uk.gov.in'] },
  { canonical: 'West Bengal', aliases: ['west bengal', 'wbpsc', 'wbcs', 'wbprb', 'wbhealth', 'kolkata', 'howrah', 'siliguri', 'durgapur'], domains: ['wb.gov.in', 'wbhealth.gov.in'] },
  { canonical: 'Chandigarh', aliases: ['chandigarh'], domains: ['chandigarh.gov.in'] },
  { canonical: 'Puducherry', aliases: ['puducherry', 'pondicherry'], domains: ['py.gov.in'] },
  { canonical: 'Ladakh', aliases: ['ladakh', 'leh', 'kargil'], domains: ['ladakh.gov.in'] },
];

const CENTRAL_INDICATOR_PATTERNS = [
  /\bstate bank of india\b/,
  /\bsbi\b/,
  /\bbank of baroda\b/,
  /\bcanara bank\b/,
  /\bcentral bank of india\b/,
  /\bindian bank\b/,
  /\bpunjab national bank\b/,
  /\bunion bank of india\b/,
  /\bbank of india\b/,
  /\bexim bank\b/,
  /\bexport-import bank of india\b/,
  /\bsidbi\b/,
  /\bsebi\b/,
  /\brbi\b/,
  /\breserve bank of india\b/,
  /\bnabard\b/,
  /\blic\b/,
  /\bibps\b/,
  /\baiims\b/,
  /\ball india institute of medical sciences\b/,
  /\btata memorial\b/,
  /\ballahabad.*university\b/,
  /\baligarh muslim university\b/,
  /\bbanaras hindu university\b/,
  /\brani lakshmi bai central agricultural\b/,
  /\brajiv gandhi national aviation\b/,
  /\biit\b/,
  /\biim\b/,
  /\bnit\b/,
  /\biiser\b/,
  /\bupsc\b/,
  /\bstaff selection commission\b/,
  /\bssc\b(?!.*(?:jkssb|uksssc|osssc|ossc|hssc|jssc|bssc|rssb|psssb|upsssc|gsssb))/,
  /\brailway\b/,
  /\brrb\b/,
  /\bdedicated freight corridor\b/,
  /\bcustoms excise\b/,
  /\bdebts recovery\b/,
  /\bisro\b/,
  /\bdepartment of space\b/,
  /\bdrdo\b/,
  /\bbarc\b/,
  /\becil\b/,
  /\belectronics corporation of india\b/,
  /\bengineers india limited\b/,
  /\beil\b/,
  /\boil india\b/,
  /\bongc\b/,
  /\bntpc\b/,
  /\bbhel\b/,
  /\bgail\b/,
  /\bsail\b/,
  /\biocl\b/,
  /\bbpcl\b/,
  /\bhpcl\b/,
  /\bmazagon dock\b/,
  /\bmunitions india\b/,
  /\badvanced weapons\b/,
  /\bordnance factory\b/,
  /\bedcil\b/,
  /\besic\b/,
  /\bepfo\b/,
  /\barmed forces tribunal\b/,
  /\bkendriya vidyalaya\b/,
  /\bnavodaya\b/,
  /\bministry of\b/,
  /\bgovernment of india\b/,
  /\ball india\b/,
  /\banywhere in india\b/,
];

const STATE_INDICATOR_PATTERNS = [
  /\b(appsc|apsc|bpsc|bssc|cgpsc|dsssb|gpsc|gsssb|hpsc|hssc|hppsc|hpscb|jkpsc|jkssb|jpsc|jssc|kpsc|kea|mppsc|mpsc|opsc|ossc|osssc|ppsc|psssb|rpsc|rssb|rsmssb|tnpsc|tspsc|tgpsc|uppsc|upsssc|ukpsc|uksssc|wbpsc)\b/,
  /\bnagar parishad\b/,
  /\bmunicipal corporation\b/,
  /\bmunicipality\b/,
  /\bcollectorate\b/,
  /\bgram panchayat\b/,
  /\bzilla parishad\b/,
  /\bdistrict child protection\b/,
  /\bdistrict programme officer\b/,
  /\bdistrict basic education\b/,
  /\bdistrict court\b/,
  /\banganwadi\b/,
  /\bkgbv\b/,
  /\bchief medical officer\b/,
  /\bstate co-?operative\b/,
  /\burban co-?operative\b/,
  /\bco-?operative bank\b/,
  /\burban health mission\b/,
  /\bstate psu\b/,
  /\bstate civil\b/,
  /\bgovernment of nct\b/,
  /\bj&k bank\b/,
  /\bjammu & kashmir bank\b/,
  /\bbrlps\b/,
  /\bc-?dit\b/,
  /\bcmd kerala\b/,
  /\bstate sports university\b/,
];

export function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/&/g, ' ')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

export function parseCalendarDateUtc(dateStr: string): number {
  const [year, month, day] = dateStr.split('-').map(Number);
  return Date.UTC(year, month - 1, day);
}

export function getTodayUtcMidnight(referenceDate = new Date()): number {
  return Date.UTC(
    referenceDate.getFullYear(),
    referenceDate.getMonth(),
    referenceDate.getDate(),
  );
}

export function getDaysDifference(dateStr: string, referenceDate = new Date()): number {
  const target = parseCalendarDateUtc(dateStr);
  const today = getTodayUtcMidnight(referenceDate);
  return Math.round((target - today) / MS_PER_DAY);
}

export function isJobActive(job: JobEntry, referenceDate = new Date()): boolean {
  if (job.data.archived) return false;
  if (!job.data.application_deadline) return true;
  return getDaysDifference(job.data.application_deadline, referenceDate) >= 0;
}

export function isJobClosingSoon(job: JobEntry, days = 7, referenceDate = new Date()): boolean {
  if (job.data.archived || !job.data.application_deadline) return false;
  const diff = getDaysDifference(job.data.application_deadline, referenceDate);
  return diff >= 0 && diff <= days;
}

export function isJobAddedRecently(job: JobEntry, days = 7, referenceDate = new Date()): boolean {
  const diff = getDaysDifference(job.data.posted_date, referenceDate);
  return diff <= 0 && diff >= -days;
}

export function isRecentDate(dateStr: string, days = 7, referenceDate = new Date()): boolean {
  const diff = getDaysDifference(dateStr, referenceDate);
  return diff <= 0 && diff >= -days;
}

export function getDeadlineBadge(
  deadline: string | null,
  referenceDate = new Date(),
  archived = false,
): { label: string; variant: 'urgent' | 'active' | 'neutral' } {
  if (archived) {
    return { label: 'Closed', variant: 'neutral' };
  }
  if (!deadline) {
    return { label: FALLBACK_TEXT, variant: 'neutral' };
  }
  const diff = getDaysDifference(deadline, referenceDate);
  if (diff < 0) {
    return { label: 'Closed', variant: 'neutral' };
  }
  if (diff === 0) {
    return { label: 'Closes Today', variant: 'urgent' };
  }
  if (diff === 1) {
    return { label: '1 Day Left', variant: 'urgent' };
  }
  if (diff <= 7) {
    return { label: `${diff} Days Left`, variant: 'urgent' };
  }
  return { label: 'Active', variant: 'active' };
}

export function tryParseCalendarDateUtc(dateStr: string | null | undefined): number | null {
  if (!dateStr) return null;
  const trimmed = dateStr.trim();
  if (!trimmed) return null;

  const isoMatch = /^(\d{4})-(\d{2})-(\d{2})(?:[T\s].*)?$/.exec(trimmed);
  if (isoMatch) {
    const [, y, m, d] = isoMatch.map(Number);
    const ms = Date.UTC(y, m - 1, d);
    return Number.isNaN(ms) ? null : ms;
  }

  const dmyMatch = /^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$/.exec(trimmed);
  if (dmyMatch) {
    const [, d, m, y] = dmyMatch.map(Number);
    if (m >= 1 && m <= 12 && d >= 1 && d <= 31) {
      const ms = Date.UTC(y, m - 1, d);
      return Number.isNaN(ms) ? null : ms;
    }
  }

  const monMap: Record<string, number> = {
    jan: 0, january: 0, feb: 1, february: 1, mar: 2, march: 2, apr: 3, april: 3,
    may: 4, jun: 5, june: 5, jul: 6, july: 6, aug: 7, august: 7, sep: 8, sept: 8,
    september: 8, oct: 9, october: 9, nov: 10, november: 10, dec: 11, december: 11,
  };
  const dMonY = /^(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})$/.exec(trimmed);
  if (dMonY && dMonY[2].toLowerCase() in monMap) {
    return Date.UTC(Number(dMonY[3]), monMap[dMonY[2].toLowerCase()], Number(dMonY[1]));
  }
  const monDY = /^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$/.exec(trimmed);
  if (monDY && monDY[1].toLowerCase() in monMap) {
    return Date.UTC(Number(monDY[3]), monMap[monDY[1].toLowerCase()], Number(monDY[2]));
  }

  return null;
}

export function formatDisplayDate(dateStr: string | null | undefined): string {
  if (!dateStr) return FALLBACK_TEXT;
  const utcMs = tryParseCalendarDateUtc(dateStr);
  if (utcMs === null) {
    return dateStr.trim() || FALLBACK_TEXT;
  }
  return new Intl.DateTimeFormat('en-GB', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(utcMs));
}

export function formatFlexibleDisplayDate(dateStr: string | null | undefined): string {
  return formatDisplayDate(dateStr);
}

export function formatVerifiedTimestamp(isoTimestamp: string): string {
  const date = new Date(isoTimestamp);
  return (
    new Intl.DateTimeFormat('en-GB', {
      day: '2-digit',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone: 'UTC',
    }).format(date) + ' UTC'
  );
}

export function formatVacancies(count: number | null): string {
  if (count === null) return FALLBACK_TEXT;
  return count.toLocaleString('en-US');
}

export function formatRupeesCompact(amount: number): string {
  if (amount >= 100000) {
    const lakhs = amount / 100000;
    const formatted = Number.isInteger(lakhs) ? String(lakhs) : lakhs.toFixed(lakhs >= 10 ? 1 : 2).replace(/\.?0+$/, '');
    return `₹${formatted}L`;
  }
  if (amount >= 1000) {
    const thousands = amount / 1000;
    const formatted = Number.isInteger(thousands) ? String(thousands) : thousands.toFixed(1).replace(/\.0$/, '');
    return `₹${formatted}k`;
  }
  return `₹${amount.toLocaleString('en-IN')}`;
}

export function displayOrFallback(value: string | null | undefined): string {
  if (!value || value.trim().length === 0) return FALLBACK_TEXT;
  return value;
}

export function sortJobsByPostedDesc(jobs: JobEntry[]): JobEntry[] {
  return [...jobs].sort(
    (a, b) => parseCalendarDateUtc(b.data.posted_date) - parseCalendarDateUtc(a.data.posted_date),
  );
}

export function sortJobsByDeadlineAsc(jobs: JobEntry[]): JobEntry[] {
  return [...jobs].sort((a, b) => {
    if (!a.data.application_deadline && !b.data.application_deadline) return 0;
    if (!a.data.application_deadline) return 1;
    if (!b.data.application_deadline) return -1;
    return (
      parseCalendarDateUtc(a.data.application_deadline) -
      parseCalendarDateUtc(b.data.application_deadline)
    );
  });
}

const SENIOR_OR_DEPUTATION_RE =
  /\b(?:retired|superannuated|re-?employment|re-?appointment|on\s+deputation|deputation\/absorption)\b/i;

const HIGH_EXPERIENCE_TITLE_RE =
  /\b(?:sr\.?\s*manager|senior\s+manager|chief\s+manager|general\s+manager|systems\s+manager|manager|jgm|dgm|gm|additional\s+professor|associate\s+professor|assistant\s+professor|professor|principal|superintending|senior\s+resident|senior\s+officer|senior\s+assistant|consultant|specialist\s+cadre\s+officer|ai\s+specialist|scientist[-\s]?(?:ii|iii|iv|[b-f])|member\s*\((?:judicial|technical)\))\b/i;

const ENTRY_POST_TITLE_RE =
  /\b(?:junior\s+clerk|driver|peon|attendant|office\s+assistant|anganwadi|sahayika|helper|apprentice|junior\s+engineer|junior\s+resident|lab\s+assistant|data\s+entry\s+operator|staff\s+nurse|danger\s+building\s+worker|dbw|10\+2|constable|sub\s+inspector)\b/i;

const MULTI_YEAR_EXP_RE =
  /(?:(?:minimum|at\s+least|min\.?|with)\s+(?:0?[2-9]|[1-9]\d)\+?\s*years?(?:'|\s+of)?(?:\s+[a-z-]+){0,4}\s+experience|\b(?:0?[3-9]|[1-9]\d)\+?\s*years?(?:'|\s+of)?(?:\s+[a-z-]+){0,4}\s+experience\b)/i;

/**
 * Classifies jobs that primarily target experienced, managerial, faculty, deputation, or retired candidates.
 * Used on the homepage stream to surface fresher / low-experience jobs first without altering date order within tiers.
 */
export function isHighExperienceJob(job: JobEntry): boolean {
  const title = job.data.title;
  const bullets = (job.data.eligibility_bullets ?? []).join(' ');
  const combined = `${title} ${job.data.summary ?? ''} ${bullets}`;

  if (SENIOR_OR_DEPUTATION_RE.test(combined)) {
    return true;
  }

  if (HIGH_EXPERIENCE_TITLE_RE.test(title) && !/\bmanagement\s+trainee\b/i.test(title)) {
    return true;
  }

  if (!ENTRY_POST_TITLE_RE.test(title) && MULTI_YEAR_EXP_RE.test(bullets)) {
    return true;
  }

  return false;
}

/**
 * Stable two-bucket partition placing low-experience / entry-level jobs before high-experience / senior jobs.
 * Preserves existing order inside each bucket rather than re-sorting by another field.
 */
export function orderJobsLowExperienceFirst(jobs: JobEntry[]): JobEntry[] {
  const lowExperience: JobEntry[] = [];
  const highExperience: JobEntry[] = [];
  for (const job of jobs) {
    if (isHighExperienceJob(job)) {
      highExperience.push(job);
    } else {
      lowExperience.push(job);
    }
  }
  return [...lowExperience, ...highExperience];
}

export interface FacetItem {
  name: string;
  slug: string;
  count: number;
  activeCount: number;
}

export function normalizeStateName(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const cleaned = raw.trim().toLowerCase();
  if (!cleaned || cleaned === 'all india' || cleaned === 'see official notification' || cleaned === 'anywhere in india') {
    return null;
  }
  for (const state of STATE_MATCHERS) {
    if (state.canonical.toLowerCase() === cleaned) return state.canonical;
    if (state.aliases.some((alias) => cleaned.includes(alias))) return state.canonical;
  }
  return null;
}

export function inferStateNameFromJob(job: JobEntry): string | null {
  const explicit = normalizeStateName(job.data.state_name);
  if (explicit) return explicit;

  const locDirect = normalizeStateName(job.data.location);
  if (locDirect) return locDirect;

  const domain = job.data.source_domain.toLowerCase();
  for (const state of STATE_MATCHERS) {
    if (state.domains.some((d) => domain.includes(d))) return state.canonical;
  }

  const combined = `${job.data.organization} ${job.data.title} ${job.data.department} ${job.data.location}`.toLowerCase();
  for (const state of STATE_MATCHERS) {
    if (
      state.aliases.some((alias) => {
        const escaped = alias.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        return new RegExp(`\\b${escaped}\\b`, 'i').test(combined);
      })
    ) {
      return state.canonical;
    }
  }
  return null;
}

export function getGovernmentLevel(job: JobEntry): GovernmentLevel {
  if (job.data.government_level === 'central' || job.data.government_level === 'state') {
    return job.data.government_level;
  }

  const org = job.data.organization.toLowerCase();
  const title = job.data.title.toLowerCase();
  const dept = job.data.department.toLowerCase();
  const domain = job.data.source_domain.toLowerCase();
  const orgAndTitle = `${org} ${title} ${domain}`;

  if (STATE_INDICATOR_PATTERNS.some((re) => re.test(orgAndTitle))) {
    return 'state';
  }
  if (CENTRAL_INDICATOR_PATTERNS.some((re) => re.test(`${orgAndTitle} ${job.data.location.toLowerCase()}`))) {
    return 'central';
  }
  if (dept.startsWith('state ')) {
    return 'state';
  }

  const locLower = job.data.location.toLowerCase();
  if (locLower.includes('all india') || locLower.includes('anywhere in india')) {
    return 'central';
  }

  if (inferStateNameFromJob(job)) {
    return 'state';
  }
  return 'central';
}

export function getGovernmentLevelLabel(level: GovernmentLevel): string {
  return level === 'central' ? 'Central Govt' : 'State Govt';
}

export function getStateName(job: JobEntry): string | null {
  if (getGovernmentLevel(job) === 'central') return null;
  return inferStateNameFromJob(job);
}

export function getAuthorityType(job: JobEntry): AuthorityType {
  if (job.data.authority_type) {
    return job.data.authority_type;
  }
  const orgTitleDomain = `${job.data.organization} ${job.data.title} ${job.data.source_domain}`.toLowerCase();
  if (
    /\b(bank|sbi|bob|canara|exim|sidbi|sebi|rbi|nabard|ibps|ecil|eil|oil india|ongc|ntpc|bhel|gail|sail|dfccil|edcil|c-dit|cmd)\b/.test(
      orgTitleDomain,
    )
  ) {
    return 'psu-bank';
  }
  if (
    /\b(police|defence|defense|army|navy|air force|armed forces|ordnance|munitions|advanced weapons|constable|sub-?inspector|cisf|bsf|crpf)\b/.test(
      orgTitleDomain,
    )
  ) {
    return 'defence-police';
  }
  if (
    /\b(teacher|professor|lecturer|librarian|university|college|kgbv|basic education|pgt|tgt|prt|iim|iit|nit)\b/.test(
      orgTitleDomain,
    )
  ) {
    return 'teaching';
  }
  return getGovernmentLevel(job) === 'state' ? 'state' : 'central';
}

export function getAuthorityLabel(type: AuthorityType): string {
  return AUTHORITY_META[type].shortLabel;
}

export function inferSalaryTierFromMonthly(minPay: number | null | undefined, maxPay: number | null | undefined): SalaryTierSlug {
  const ref = minPay && minPay > 0 ? minPay : maxPay && maxPay > 0 ? maxPay : null;
  if (ref === null) return 'not-specified';
  if (ref <= 25000) return 'up-to-25k';
  if (ref <= 45000) return '25k-45k';
  if (ref <= 70000) return '45k-70k';
  if (ref <= 100000) return '70k-1l';
  return 'above-1l';
}

export function getSalaryTier(job: JobEntry): SalaryTierSlug {
  if (job.data.salary_tier && job.data.salary_tier !== 'not-specified') {
    return job.data.salary_tier;
  }
  return inferSalaryTierFromMonthly(job.data.salary_min, job.data.salary_max);
}

export function formatSalaryBadge(job: JobEntry): {
  headline: string;
  subline: string | null;
  isSpecified: boolean;
} {
  const minPay = job.data.salary_min ?? null;
  const maxPay = job.data.salary_max ?? null;
  const payLevel = job.data.pay_level ?? null;

  if (minPay && maxPay && maxPay > minPay) {
    return {
      headline: `${formatRupeesCompact(minPay)} – ${formatRupeesCompact(maxPay)}`,
      subline: payLevel,
      isSpecified: true,
    };
  }
  if (minPay && minPay > 0) {
    return {
      headline: `${formatRupeesCompact(minPay)}/mo`,
      subline: payLevel,
      isSpecified: true,
    };
  }
  if (maxPay && maxPay > 0) {
    return {
      headline: `Up to ${formatRupeesCompact(maxPay)}`,
      subline: payLevel,
      isSpecified: true,
    };
  }
  if (payLevel) {
    return {
      headline: payLevel,
      subline: null,
      isSpecified: true,
    };
  }
  return {
    headline: 'Visit Official Site',
    subline: null,
    isSpecified: false,
  };
}

export function getSalarySortValue(job: JobEntry): number {
  return job.data.salary_max ?? job.data.salary_min ?? 0;
}

export function getSalaryTierFacets(jobs: JobEntry[], referenceDate = new Date()) {
  return SALARY_TIERS.map((tier) => {
    const matching = jobs.filter((job) => getSalaryTier(job) === tier.slug);
    const active = matching.filter((job) => isJobActive(job, referenceDate));
    const vacancies = active.reduce((sum, j) => sum + (j.data.total_vacancies ?? 0), 0);
    return {
      ...tier,
      totalCount: matching.length,
      activeCount: active.length,
      vacancies,
    };
  });
}

export function getAuthorityFacets(jobs: JobEntry[], referenceDate = new Date()): FacetItem[] {
  const order: AuthorityType[] = ['central', 'state', 'psu-bank', 'defence-police', 'teaching'];
  return order.map((slug) => {
    const matching = jobs.filter((job) => getAuthorityType(job) === slug);
    const active = matching.filter((job) => isJobActive(job, referenceDate));
    return {
      name: AUTHORITY_META[slug].label,
      slug,
      count: matching.length,
      activeCount: active.length,
    };
  });
}

export function getStateFacets(jobs: JobEntry[], referenceDate = new Date()): FacetItem[] {
  const map = new Map<string, FacetItem>();
  for (const job of jobs) {
    const stateName = getStateName(job);
    if (!stateName) continue;
    const slug = slugify(stateName);
    const existing = map.get(slug) ?? { name: stateName, slug, count: 0, activeCount: 0 };
    existing.count += 1;
    if (isJobActive(job, referenceDate)) existing.activeCount += 1;
    map.set(slug, existing);
  }
  return Array.from(map.values()).sort((a, b) => a.name.localeCompare(b.name));
}

export function getJurisdictionSummary(jobs: JobEntry[], referenceDate = new Date()) {
  let centralTotal = 0;
  let centralActive = 0;
  let stateTotal = 0;
  let stateActive = 0;
  for (const job of jobs) {
    const active = isJobActive(job, referenceDate);
    if (getGovernmentLevel(job) === 'central') {
      centralTotal += 1;
      if (active) centralActive += 1;
    } else {
      stateTotal += 1;
      if (active) stateActive += 1;
    }
  }
  return { centralTotal, centralActive, stateTotal, stateActive };
}

export function getDepartmentFacets(jobs: JobEntry[], referenceDate = new Date()): FacetItem[] {
  const map = new Map<string, FacetItem>();
  for (const job of jobs) {
    const name = job.data.department;
    const slug = slugify(name);
    const existing = map.get(slug) ?? { name, slug, count: 0, activeCount: 0 };
    existing.count += 1;
    if (isJobActive(job, referenceDate)) existing.activeCount += 1;
    map.set(slug, existing);
  }
  return Array.from(map.values()).sort((a, b) => a.name.localeCompare(b.name));
}

export function getQualificationFacets(jobs: JobEntry[], referenceDate = new Date()): FacetItem[] {
  const map = new Map<string, FacetItem>();
  for (const job of jobs) {
    const name = job.data.qualification_level;
    const slug = slugify(name);
    const existing = map.get(slug) ?? { name, slug, count: 0, activeCount: 0 };
    existing.count += 1;
    if (isJobActive(job, referenceDate)) existing.activeCount += 1;
    map.set(slug, existing);
  }
  return Array.from(map.values()).sort((a, b) => a.name.localeCompare(b.name));
}

export function getRelatedJobs(
  current: JobEntry,
  allJobs: JobEntry[],
  limit = 4,
  referenceDate = new Date(),
): JobEntry[] {
  const currentLevel = getGovernmentLevel(current);
  const currentState = getStateName(current);
  const currentTier = getSalaryTier(current);
  const candidates = allJobs.filter(
    (job) => job.data.slug !== current.data.slug && isJobActive(job, referenceDate),
  );
  const scored = candidates.map((job) => {
    let score = 0;
    if (currentTier !== 'not-specified' && getSalaryTier(job) === currentTier) score += 3;
    if (job.data.department === current.data.department) score += 2;
    if (job.data.qualification_level === current.data.qualification_level) score += 2;
    if (getGovernmentLevel(job) === currentLevel) score += 1;
    if (currentState && getStateName(job) === currentState) score += 2;
    return { job, score };
  });
  scored.sort(
    (a, b) =>
      b.score - a.score ||
      parseCalendarDateUtc(b.job.data.posted_date) - parseCalendarDateUtc(a.job.data.posted_date),
  );
  return scored.slice(0, limit).map((item) => item.job);
}
