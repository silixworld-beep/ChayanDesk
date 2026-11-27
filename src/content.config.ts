import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { z } from 'astro/zod';

const importantDateSchema = z.object({
  label: z.string().min(1),
  date: z.string().min(1),
});

const vacancyBreakdownSchema = z.object({
  category: z.string().min(1),
  vacancies: z.number().int().nonnegative().nullable(),
  eligibility: z.string().nullish(),
  notes: z.string().nullish(),
});

const feeStructureSchema = z.object({
  category: z.string().min(1),
  amount: z.string().min(1),
});

const importantLinkSchema = z.object({
  label: z.string().min(2),
  url: z.url(),
  category: z.enum(['result', 'admit-card', 'answer-key']).nullish(),
  added_at: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).nullish(),
});

const salaryTierEnum = z.enum([
  'up-to-25k',
  '25k-45k',
  '45k-70k',
  '70k-1l',
  'above-1l',
  'not-specified',
]);

const authorityTypeEnum = z.enum([
  'central',
  'state',
  'psu-bank',
  'defence-police',
  'teaching',
]);

const jobs = defineCollection({
  loader: glob({ pattern: '**/*.json', base: './src/data/jobs' }),
  schema: z.object({
    slug: z.string().regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/),
    chayandesk_url: z.url().nullish(),
    title: z.string().min(3),
    organization: z.string().min(2),
    department: z.string().min(2),
    government_level: z.enum(['central', 'state']).optional(),
    state_name: z.string().min(2).nullable().optional(),
    location: z.string().min(2),
    qualification_level: z.string().min(2),
    total_vacancies: z.number().int().positive().nullable(),
    salary_range: z.string().min(1).nullable(),
    salary_min: z.number().int().nonnegative().nullable().optional(),
    salary_max: z.number().int().nonnegative().nullable().optional(),
    pay_level: z.string().min(1).nullable().optional(),
    salary_tier: salaryTierEnum.default('not-specified'),
    authority_type: authorityTypeEnum.optional(),
    exam_group_id: z.string().min(2).nullable().optional(),
    application_fee: z.string().min(1).nullable(),
    age_limit: z.string().min(1).nullable(),
    important_dates: z.array(importantDateSchema),
    application_deadline: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).nullable(),
    posted_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
    last_verified_at: z.iso.datetime({ offset: true }),
    summary: z.string().min(20),
    eligibility_bullets: z.array(z.string().min(3)),
    vacancy_breakdown: z.array(vacancyBreakdownSchema),
    how_to_apply_steps: z.array(z.string().min(3)),
    official_apply_url: z.url(),
    official_pdf_url: z.url().nullable(),
    important_links: z.array(importantLinkSchema).optional(),
    homepage_menus: z.array(z.enum(['result', 'admit-card', 'answer-key'])).optional(),
    source_domain: z.string().min(3),
    source_url: z.url().optional(),
    notification_code: z.string().optional(),
    fee_structure: z.array(feeStructureSchema).optional(),
    archived: z.boolean().default(false),
    archived_at: z.iso.datetime({ offset: true }).optional(),
  }),
});

const admissions = defineCollection({
  loader: glob({ pattern: '**/*.json', base: './src/data/admissions' }),
  schema: z.object({
    slug: z.string().regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/),
    chayandesk_url: z.url().nullish(),
    title: z.string().min(3),
    organization: z.string().min(2),
    course_level: z.string().min(2),
    location: z.string().min(2),
    application_deadline: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).nullable(),
    posted_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
    last_verified_at: z.iso.datetime({ offset: true }).optional(),
    exam_date: z.string().min(3),
    application_fee: z.string().min(1).nullable(),
    age_limit: z.string().min(1).nullable().optional(),
    notification_code: z.string().optional(),
    fee_structure: z.array(feeStructureSchema).optional(),
    summary: z.string().min(20).optional(),
    eligibility_bullets: z.array(z.string().min(3)),
    important_dates: z.array(importantDateSchema),
    how_to_apply_steps: z.array(z.string().min(3)).optional(),
    important_links: z.array(importantLinkSchema).optional(),
    homepage_menus: z.array(z.enum(['result', 'admit-card', 'answer-key'])).optional(),
    official_apply_url: z.url(),
    official_brochure_pdf: z.url().nullable(),
    source_domain: z.string().min(3),
    exam_group_id: z.string().min(2).nullable().optional(),
  }),
});

const guides = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/guides' }),
  schema: z.object({
    title: z.string().min(5),
    description: z.string().min(15),
    published_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
    updated_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
    category: z.string().min(2),
    reading_time: z.string().min(2),
    author: z.string().default('Editorial Verification Desk'),
  }),
});

export const collections = {
  jobs,
  admissions,
  guides,
};
