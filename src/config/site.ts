export type SalaryTierSlug =
  | 'up-to-25k'
  | '25k-45k'
  | '45k-70k'
  | '70k-1l'
  | 'above-1l'
  | 'not-specified';

export type AuthorityType =
  | 'central'
  | 'state'
  | 'psu-bank'
  | 'defence-police'
  | 'teaching';

export interface SalaryTierConfig {
  slug: SalaryTierSlug;
  label: string;
  shortLabel: string;
  payLevelHint: string;
  description: string;
  minMonthly: number | null;
  maxMonthly: number | null;
}

export const SITE_CONFIG = {
  brandName: 'SarkariIndex',
  shortMark: 'SI',
  tagline: 'Verified Public Sector Recruitment',
  authorityBarLeft:
    '★ 100% GOVERNMENT & PSU JOBS ONLY • ZERO PRIVATE LISTINGS • DIRECT .GOV.IN LINKS',
  positioningTagline:
    '100% GOVERNMENT & PSU JOBS ONLY • ZERO PRIVATE LISTINGS • DIRECT OFFICIAL PORTAL LINKS',
  dailySyncLabel: 'Daily Sync: 6:00 AM IST',
  siteUrl: 'https://sarkariindex.pages.dev',
  correctionsEmail: 'silixworld@gmail.com',
  generalEmail: 'silixworld@gmail.com',
  navLinks: [
    { href: '/jobs', label: 'Latest Jobs' },
    { href: '/central-jobs', label: 'Central Govt' },
    { href: '/states', label: 'State Govt' },
    { href: '/closing-soon', label: 'Closing Soon' },
    { href: '/admissions', label: 'Admissions' },
    { href: '/guides', label: 'Exam Guides' },
  ],
  mobileIntentLinks: [
    { href: '/jobs', label: 'Jobs' },
    { href: '/central-jobs', label: 'Central' },
    { href: '/states', label: 'States' },
    { href: '/closing-soon', label: 'Closing' },
    { href: '/admissions', label: 'Admissions' },
    { href: '/guides', label: 'Guides' },
    { href: '/jobs/by-salary', label: '₹ Salary' },
  ],
} as const;

export const SALARY_TIERS: SalaryTierConfig[] = [
  {
    slug: 'up-to-25k',
    label: 'Up to ₹25,000',
    shortLabel: 'Up to ₹25k',
    payLevelHint: 'Level 1–2 (7th CPC)',
    description:
      'Entry-level Group C, MTS, Apprentice, Constable, Anganwadi, and Tradesman pay scales (₹18,000–₹25,000 initial monthly pay).',
    minMonthly: 0,
    maxMonthly: 25000,
  },
  {
    slug: '25k-45k',
    label: '₹25k – ₹45k',
    shortLabel: '₹25k–₹45k',
    payLevelHint: 'Level 3–5 (7th CPC)',
    description:
      'Clerk, Steno, Junior Assistant, Technician, Patwari, and State Subordinate Service pay brackets (₹25,001–₹45,000 entry pay).',
    minMonthly: 25001,
    maxMonthly: 45000,
  },
  {
    slug: '45k-70k',
    label: '₹45k – ₹70k',
    shortLabel: '₹45k–₹70k',
    payLevelHint: 'Level 6–8 (7th CPC)',
    description:
      'Sub-Inspector, Junior Engineer, Officer Scale-I, Bank PO, ASO, and Nursing Officer pay scales (₹45,001–₹70,000 entry pay).',
    minMonthly: 45001,
    maxMonthly: 70000,
  },
  {
    slug: '70k-1l',
    label: '₹70k – ₹1 Lakh',
    shortLabel: '₹70k–₹1L',
    payLevelHint: 'Level 9–10 (7th CPC)',
    description:
      'Group A Gazetted Officers, Assistant Professor, PSU Management Trainee, and Senior Bank Officer scales (₹70,001–₹1,00,000 entry pay).',
    minMonthly: 70001,
    maxMonthly: 100000,
  },
  {
    slug: 'above-1l',
    label: '₹1 Lakh+',
    shortLabel: '₹1 Lakh+',
    payLevelHint: 'Level 11+ (7th CPC)',
    description:
      'AIIMS Faculty, Senior Medical Specialist, Associate/Full Professor, Executive PSU, and Senior Grade A scales (above ₹1,00,000/month).',
    minMonthly: 100001,
    maxMonthly: null,
  },
];

export const ALL_SALARY_TIER_META: Record<SalaryTierSlug, SalaryTierConfig> = {
  'up-to-25k': SALARY_TIERS[0],
  '25k-45k': SALARY_TIERS[1],
  '45k-70k': SALARY_TIERS[2],
  '70k-1l': SALARY_TIERS[3],
  'above-1l': SALARY_TIERS[4],
  'not-specified': {
    slug: 'not-specified',
    label: 'Visit Official Site',
    shortLabel: 'Visit Official Site',
    payLevelHint: 'Visit Official Site',
    description:
      'Notifications whose detailed pay matrix grade appears inside the issuing PDF circular rather than headline summary text.',
    minMonthly: null,
    maxMonthly: null,
  },
};

export const AUTHORITY_META: Record<
  AuthorityType,
  { slug: AuthorityType; label: string; shortLabel: string }
> = {
  central: { slug: 'central', label: 'Central Government', shortLabel: 'Central Govt' },
  state: { slug: 'state', label: 'State Government', shortLabel: 'State Govt' },
  'psu-bank': { slug: 'psu-bank', label: 'PSU & Public Banking', shortLabel: 'PSU / Bank' },
  'defence-police': {
    slug: 'defence-police',
    label: 'Defence & Police Services',
    shortLabel: 'Defence / Police',
  },
  teaching: {
    slug: 'teaching',
    label: 'Teaching & Universities',
    shortLabel: 'Teaching / Univ',
  },
};
