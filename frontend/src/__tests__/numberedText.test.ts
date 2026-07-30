import {
  formatNumberedExpected,
  splitNumberedItems,
  splitNumberedItemsNonEmpty,
} from '../utils/numberedText';

describe('numberedText', () => {
  it('omits empty expected slots when serializing', () => {
    const steps = [
      { parsed_result: '' },
      { parsed_result: '' },
      { parsed_result: '出现【我的反馈记录】' },
    ];
    expect(formatNumberedExpected(steps)).toBe('3. 出现【我的反馈记录】');
  });

  it('parses sparse expected by index into aligned slots', () => {
    const text = '9. 出现【我的反馈记录】';
    const parts = splitNumberedItems(text);
    expect(parts).toHaveLength(9);
    expect(parts.slice(0, 8).every((p) => p === '')).toBe(true);
    expect(parts[8]).toBe('出现【我的反馈记录】');
  });

  it('treats bare numbering as empty', () => {
    expect(splitNumberedItems('1.\n2.\n3.')).toEqual([]);
    expect(splitNumberedItemsNonEmpty('1. \n2. \n3. 有内容')).toEqual(['有内容']);
  });

  it('does not keep junk numbering-only bodies when formatting', () => {
    const steps = [
      { parsed_result: '2.' },
      { parsed_result: '4.' },
      { parsed_result: '出现【完成】' },
    ];
    expect(formatNumberedExpected(steps)).toBe('3. 出现【完成】');
  });
});
