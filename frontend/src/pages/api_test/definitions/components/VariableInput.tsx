import React from 'react';
import { Input } from '@arco-design/web-react';
import styles from '../style/index.module.less';

interface VariableInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  /** 是否显示 {{var}} 高亮预览 */
  showPreview?: boolean;
}

/** 提取 {{var}} 变量 token */
const VAR_REGEX = /\{\{\s*[\w.$-]+\s*\}\}/g;

/**
 * 变量输入框：普通输入 + {{var}} 高亮预览。
 * 高亮仅用于视觉提示，实际值仍为纯文本（后端渲染）。
 */
const VariableInput: React.FC<VariableInputProps> = ({
  value,
  onChange,
  placeholder,
  showPreview = true,
}) => {
  const renderHighlight = (): React.ReactNode => {
    const parts: React.ReactNode[] = [];
    let lastIndex = 0;
    let match: RegExpExecArray | null;
    const regex = new RegExp(VAR_REGEX.source, 'g');
    let key = 0;
    while ((match = regex.exec(value)) !== null) {
      if (match.index > lastIndex) {
        parts.push(<span key={key++}>{value.slice(lastIndex, match.index)}</span>);
      }
      parts.push(
        <span key={key++} className={styles.varToken}>
          {match[0]}
        </span>
      );
      lastIndex = match.index + match[0].length;
    }
    if (lastIndex < value.length) {
      parts.push(<span key={key++}>{value.slice(lastIndex)}</span>);
    }
    return parts;
  };

  return (
    <div className={styles.varInputWrap}>
      <Input
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        allowClear
      />
      {showPreview && value && (
        <div className={styles.varPreview}>{renderHighlight()}</div>
      )}
    </div>
  );
};

export default VariableInput;