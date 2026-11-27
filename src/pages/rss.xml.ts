import rss from '@astrojs/rss';
import type { APIRoute } from 'astro';
import { getCollection } from 'astro:content';
import { SITE_CONFIG } from '../config/site';
import {
  SITE_NAME,
  getGovernmentLevel,
  getGovernmentLevelLabel,
  getStateName,
  isJobActive,
  sortJobsByPostedDesc,
} from '../lib/jobs';

export const GET: APIRoute = async (context) => {
  const jobs = sortJobsByPostedDesc(
    (await getCollection('jobs')).filter((job) => isJobActive(job)),
  );

  return rss({
    title: `${SITE_NAME} — Public Sector Recruitment Notices`,
    description:
      'Verified central government, state government, PSU, railway, banking, and statutory recruitment notices with direct official portal links.',
    site: context.site ?? SITE_CONFIG.siteUrl,
    items: jobs.map((job) => {
      const govLevel = getGovernmentLevel(job);
      const stateName = getStateName(job);
      return {
        title: job.data.title,
        description: `${job.data.organization} (${job.data.qualification_level}) — ${job.data.summary}`,
        pubDate: new Date(`${job.data.posted_date}T00:00:00Z`),
        link: `/jobs/${job.data.slug}`,
        categories: [
          getGovernmentLevelLabel(govLevel),
          ...(stateName ? [stateName] : []),
          job.data.department,
          job.data.qualification_level,
        ],
      };
    }),
    customData: '<language>en-us</language>',
  });
};
