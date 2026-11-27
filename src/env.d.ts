/// <reference types="astro/client" />

interface ImportMetaEnv {
  readonly PUBLIC_ADS_ENABLED?: string;
  readonly PUBLIC_ADS_PREVIEW?: string;
  readonly PUBLIC_ADSENSE_CLIENT?: string;
  readonly PUBLIC_ADSENSE_SLOT_LEFT_RAIL?: string;
  readonly PUBLIC_ADSENSE_SLOT_RIGHT_RAIL?: string;
  readonly PUBLIC_ADSENSE_SLOT_LEADERBOARD?: string;
  readonly PUBLIC_ADSENSE_SLOT_IN_FEED?: string;
  readonly PUBLIC_ADSENSE_SLOT_SIDEBAR?: string;
  readonly PUBLIC_ADSENSE_SLOT_DETAIL_MID?: string;
  readonly PUBLIC_SITE_URL?: string;
  readonly VERCEL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
