export interface TradingViewStudyConfig {
  id: string;
  inputs?: Record<string, unknown>;
}

// Prefer a widget-compatible study id format for Smart Money Concepts.
// PUB;* pine script ids are not consistently supported by the external embed widget.
export const SMART_MONEY_CONCEPTS_STUDY_ID = 'STD;Smart%1Money%1Concepts';

const STUDY_ID_ALIASES: Record<string, string> = {
  'STD;SMART%1MONEY%1CONCEPTS': SMART_MONEY_CONCEPTS_STUDY_ID,
  'STD;SMART MONEY CONCEPTS': SMART_MONEY_CONCEPTS_STUDY_ID,
  'PUB;SMART%1MONEY%1CONCEPTS': SMART_MONEY_CONCEPTS_STUDY_ID,
  'PUB;CNB3FSPH': SMART_MONEY_CONCEPTS_STUDY_ID,
  'SMART MONEY CONCEPTS': SMART_MONEY_CONCEPTS_STUDY_ID,
  SMC: SMART_MONEY_CONCEPTS_STUDY_ID,
};

function canonicalizeStudyPrefix(value: string): string {
  if (/^STD;/i.test(value)) return `STD;${value.slice(4)}`;
  if (/^PUB;/i.test(value)) return `PUB;${value.slice(4)}`;
  return value;
}

export function normalizeTradingViewStudyId(rawId: string): string | null {
  const trimmed = rawId.trim();
  if (!trimmed) return null;

  const alias = STUDY_ID_ALIASES[trimmed.toUpperCase()];
  let candidate = canonicalizeStudyPrefix(alias ?? trimmed);

  // Keep modern TradingView built-in format untouched (e.g. RSI@tv-basicstudies).
  if (candidate.includes('@')) {
    return candidate;
  }

  if (!candidate.startsWith('STD;') && !candidate.startsWith('PUB;')) {
    candidate = `STD;${candidate}`;
  }

  const separator = candidate.indexOf(';');
  if (separator < 0) return null;

  const prefix = candidate.slice(0, separator).toUpperCase();
  const body = candidate.slice(separator + 1).trim();
  if (!body) return null;

  return `${prefix};${body.replace(/\s+/g, '%1')}`;
}

export function normalizeTradingViewStudies(
  studies: TradingViewStudyConfig[]
): TradingViewStudyConfig[] {
  const seen = new Set<string>();
  const normalized: TradingViewStudyConfig[] = [];

  for (const study of studies) {
    const id = normalizeTradingViewStudyId(study.id);
    if (!id || seen.has(id)) continue;

    seen.add(id);
    if (study.inputs && Object.keys(study.inputs).length > 0) {
      normalized.push({ id, inputs: study.inputs });
    } else {
      normalized.push({ id });
    }
  }

  return normalized;
}

export function formatTradingViewStudyLabel(studyId: string): string {
  const normalized = normalizeTradingViewStudyId(studyId);
  if (!normalized) return studyId;

  if (normalized === SMART_MONEY_CONCEPTS_STUDY_ID) {
    return 'Smart Money Concepts';
  }

  if (normalized.includes('@')) {
    return normalized.replace(/@tv-basicstudies$/i, '').replace(/_/g, ' ');
  }

  return normalized.replace(/^(STD|PUB);/i, '').replace(/%1/g, ' ');
}
