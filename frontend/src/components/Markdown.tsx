import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Renders the assistant answer. react-markdown escapes raw HTML by default,
 * so model output is treated as plain Markdown, never as markup.
 */
const components: Components = {
  h1: ({ children }) => <h3 className="md__heading">{children}</h3>,
  h2: ({ children }) => <h4 className="md__heading">{children}</h4>,
  h3: ({ children }) => <h4 className="md__heading">{children}</h4>,
  h4: ({ children }) => <h5 className="md__heading">{children}</h5>,
  p: ({ children }) => <p className="md__paragraph">{children}</p>,
  ul: ({ children }) => <ul className="md__list">{children}</ul>,
  ol: ({ children }) => <ol className="md__list md__list--ordered">{children}</ol>,
  li: ({ children }) => <li className="md__item">{children}</li>,
  strong: ({ children }) => <strong className="md__strong">{children}</strong>,
  em: ({ children }) => <em className="md__em">{children}</em>,
  a: ({ children, href }) => (
    <a className="md__link" href={href} target="_blank" rel="noreferrer">
      {children}
    </a>
  ),
  code: ({ children }) => <code className="md__code">{children}</code>,
  blockquote: ({ children }) => (
    <blockquote className="md__quote">{children}</blockquote>
  ),
  hr: () => <hr className="md__rule" />,
  table: ({ children }) => (
    <div className="md__table-wrap">
      <table className="md__table">{children}</table>
    </div>
  ),
};

export function Markdown({ source }: { source: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {source}
      </ReactMarkdown>
    </div>
  );
}
