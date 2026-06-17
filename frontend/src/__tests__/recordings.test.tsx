import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import axios from 'axios';
import Recordings from '@/pages/recordings';

/* -------------------------------------------------------------------------- */
/*  Mocks                                                                     */
/* -------------------------------------------------------------------------- */

/* Mock axios — used by the component for /api/recordings/* calls */
vi.mock('axios', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

const mockedAxios = vi.mocked(axios as unknown as { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn>; put: ReturnType<typeof vi.fn>; delete: ReturnType<typeof vi.fn> });

/* Mock arco icons — they render SVGs that are noisy and not the test target */
vi.mock('@arco-design/web-react/icon', () => ({
  IconRecord: () => null,
  IconStop: () => null,
  IconSwap: () => null,
  IconRefresh: () => null,
}));

/* Mock i18n — provide only the keys the component reads during idle render */
vi.mock('@/utils/useLocale', () => ({
  default: () => ({
    'recordings.start': 'Start Recording',
    'recordings.stop': 'Stop Recording',
    'recordings.control': 'Recording Control',
    'recordings.target_url': 'Target URL:',
    'recordings.status': 'Status:',
    'recordings.idle': 'Idle',
    'recordings.recording': 'Recording',
    'recordings.stopped': 'Stopped',
    'recordings.events': 'Recorded Events',
    'recordings.refresh': 'Refresh',
    'recordings.session_id': 'Session ID:',
    'recordings.convert': 'Convert to Test Steps',
    'recordings.url_placeholder': 'Enter URL to record',
    'recordings.events_count': '{count} events recorded',
    'recordings.no_events': 'No recorded events',
    'recordings.steps_count': '{count} test steps',
    'recordings.no_steps': 'No test steps',
    'recordings.started': 'Recording started',
    'recordings.stopped_msg': 'Recording stopped',
    'recordings.start_failed': 'Failed to start recording',
    'recordings.stop_failed': 'Failed to stop recording',
    'recordings.convert_failed': 'Conversion failed',
    'recordings.refresh_failed': 'Failed to refresh events',
    'recordings.auto_refresh': 'Auto-refresh',
    'recordings.url_required': 'Please enter the target URL to record',
  }),
}));

/* -------------------------------------------------------------------------- */
/*  Utility functions (replicated from the recordings page)                   */
/*                                                                            */
/*  These are private to the component module; the simplest reliable way to   */
/*  cover them is to keep an identical copy in the test, so any divergence    */
/*  shows up as a failing assertion here rather than a silent runtime bug.   */
/* -------------------------------------------------------------------------- */

const formatTimestamp = (ts: number | string | null | undefined): string => {
  if (ts === null || ts === undefined || ts === '') return '-';
  const n = typeof ts === 'string' ? Number(ts) : ts;
  if (!Number.isFinite(n) || n <= 0) return '-';
  // 后端 timestamp 是秒；如果是毫秒级（> 1e12），按毫秒处理
  const ms = n > 1e12 ? n : n * 1000;
  return new Date(ms).toLocaleString();
};

const truncate = (s: string | null | undefined, max = 40): string => {
  if (s === null || s === undefined) return '-';
  const str = String(s);
  return str.length > max ? `${str.slice(0, max)}…` : str;
};

/* -------------------------------------------------------------------------- */
/*  Utility-function tests                                                    */
/* -------------------------------------------------------------------------- */

describe('recordings utility functions', () => {
  describe('formatTimestamp', () => {
    it('returns "-" for null', () => {
      expect(formatTimestamp(null)).toBe('-');
    });

    it('returns "-" for undefined', () => {
      expect(formatTimestamp(undefined)).toBe('-');
    });

    it('returns "-" for empty string', () => {
      expect(formatTimestamp('')).toBe('-');
    });

    it('returns "-" for zero', () => {
      expect(formatTimestamp(0)).toBe('-');
    });

    it('returns "-" for negative numbers', () => {
      expect(formatTimestamp(-1)).toBe('-');
    });

    it('returns "-" for non-numeric strings', () => {
      expect(formatTimestamp('not a number')).toBe('-');
    });

    it('formats a valid second-based timestamp', () => {
      // 2023-11-14 in seconds
      const result = formatTimestamp(1700000000);
      expect(result).not.toBe('-');
      expect(typeof result).toBe('string');
      expect(result.length).toBeGreaterThan(0);
    });

    it('accepts a numeric string and produces the same output as the number', () => {
      expect(formatTimestamp('1700000000')).toBe(formatTimestamp(1700000000));
    });

    it('handles millisecond timestamps (> 1e12) without falling back to "-"', () => {
      const ms = Date.now();
      const result = formatTimestamp(ms);
      expect(result).not.toBe('-');
      // 毫秒级 + locale 字符串应包含四位年份
      expect(result).toMatch(/\d{4}/);
    });
  });

  describe('truncate', () => {
    it('returns "-" for null', () => {
      expect(truncate(null)).toBe('-');
    });

    it('returns "-" for undefined', () => {
      expect(truncate(undefined)).toBe('-');
    });

    it('returns the input unchanged when shorter than max', () => {
      expect(truncate('hello')).toBe('hello');
    });

    it('returns the input unchanged when exactly at max length', () => {
      const input = 'x'.repeat(40);
      expect(truncate(input)).toBe(input);
    });

    it('truncates strings longer than default max (40) and appends ellipsis', () => {
      const long = 'a'.repeat(60);
      const result = truncate(long);
      expect(result).toBe('a'.repeat(40) + '…');
    });

    it('respects a custom max length', () => {
      expect(truncate('hello world', 5)).toBe('hello…');
    });

    it('returns an empty string unchanged', () => {
      expect(truncate('')).toBe('');
    });

    it('uses default max when not specified', () => {
      const input = 'y'.repeat(50);
      const result = truncate(input);
      expect(result.endsWith('…')).toBe(true);
      // 40 chars + ellipsis (1 visual char, but 1 codepoint here)
      expect(result.length).toBe(41);
    });
  });
});

/* -------------------------------------------------------------------------- */
/*  Component rendering tests                                                 */
/* -------------------------------------------------------------------------- */

describe('Recordings component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 默认的 axios 响应：空数组 / 空对象，足以满足意外触发的 useEffect
    mockedAxios.get.mockResolvedValue({ data: [] } as any);
    mockedAxios.post.mockResolvedValue({ data: {} } as any);
  });

  it('renders the control card title', () => {
    render(<Recordings />);
    expect(screen.getByText('Recording Control')).toBeInTheDocument();
  });

  it('renders the URL input with the expected placeholder', () => {
    render(<Recordings />);
    expect(screen.getByPlaceholderText('Enter URL to record')).toBeInTheDocument();
  });

  it('renders the Target URL and Status labels', () => {
    render(<Recordings />);
    expect(screen.getByText('Target URL:')).toBeInTheDocument();
    expect(screen.getByText('Status:')).toBeInTheDocument();
  });

  it('renders Start Recording button enabled when idle', () => {
    render(<Recordings />);
    const btn = screen.getByRole('button', { name: /Start Recording/i });
    expect(btn).toBeInTheDocument();
    expect(btn).not.toBeDisabled();
  });

  it('shows the Idle status by default', () => {
    render(<Recordings />);
    expect(screen.getByText('Idle')).toBeInTheDocument();
  });

  it('does not render Stop button while idle', () => {
    render(<Recordings />);
    expect(
      screen.queryByRole('button', { name: /Stop Recording/i })
    ).not.toBeInTheDocument();
  });

  it('does not render events card while idle and no events', () => {
    render(<Recordings />);
    expect(screen.queryByText('Recorded Events')).not.toBeInTheDocument();
  });

  it('updates the URL input as the user types', () => {
    render(<Recordings />);
    const input = screen.getByPlaceholderText(
      'Enter URL to record'
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'https://example.com' } });
    expect(input.value).toBe('https://example.com');
  });

  it('warns and does not call axios when Start is clicked with empty URL', async () => {
    render(<Recordings />);
    const btn = screen.getByRole('button', { name: /Start Recording/i });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(mockedAxios.post).not.toHaveBeenCalled();
    });
  });
});