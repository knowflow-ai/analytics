import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpenText, ClipboardCheck, MessageSquareText, X } from "lucide-react";
import { useMemo, useState } from "react";
import {
  listQueryFailures,
  updateQueryFailureStatus,
} from "@analytics/api/analytics";
import type { AnalyticsFeedbackStatus } from "@analytics/api/types";
import { Badge, Button, Empty, Spinner } from "@analytics/components/ui";
import {
  feedbackEmptyCopy,
  feedbackFixTarget,
  feedbackRows,
  type FeedbackCatalogIndex,
  type FeedbackFixTarget,
  type FeedbackRow,
  type FeedbackKind,
} from "./ask-feedback-state";
import { ANALYTICS_TASK_PANEL_CLASS } from "@analytics/lib/layout";
import type { WorkbenchContext } from "./index";

type Context = Pick<WorkbenchContext, "projectId" | "revision" | "readOnly">;

/** 三种收场的说人话标签。用户读到的必须是"发生了什么"，不是内部状态名。 */
const KIND_LABEL: Record<FeedbackKind, string> = {
  clarified: "用户替系统补了答案",
  inferred: "模型自己猜的",
  unknown_value: "说了系统不认识的词",
  refused: "没答上来",
  // 前四类是系统自己察觉到的异常；这两类只有用户知道——查询成功、治理关全绿，
  // 数字也出来了，答得对不对系统看不出来。
  disliked: "用户说答错了",
  liked: "用户说答得对",
};

const KIND_TONE: Record<FeedbackKind, "green" | "violet" | "amber" | "blue" | "red"> = {
  clarified: "green",
  inferred: "violet",
  unknown_value: "amber",
  refused: "blue",
  disliked: "red",
  liked: "green",
};

const TARGET_LABEL: Record<FeedbackFixTarget["kind"], string> = {
  metric: "指标",
  dimension: "维度",
  dimensionValue: "维度值",
};

/**
 * 问数反馈：线上真实提问回流到建模。
 *
 * 这一页只回答一个问题：**下一版该补哪些说法？**
 *
 * 早先它只是一张四列宽表：证据摆在那里，但看完不知道下一步点哪里——想补一条别名，
 * 得自己记住成员名，切到语义建模，再在业务词典里翻出来。所以每条带正解的证据都给
 * 一个落点：「补进词典」直接打开对应成员的术语/维度值编辑器。
 *
 * 治理边界没变：这里仍然不改任何东西，编辑发生在业务词典里，走草稿版本与发布流程
 * ——一次线上提问不是长期业务口径。落点也绝不猜：只有 `resolution` 在当前草稿目录里
 * 恰好命中一个成员时才给按钮（见 `feedbackFixTarget`）。
 */
const PAGE_SIZE = 50;

export function AskFeedbackPanel({
  projectId,
  revision,
  readOnly,
  onFixInDictionary,
  onAddToEvaluation,
}: Context & {
  onFixInDictionary: (target: FeedbackFixTarget) => void;
  /** 点赞行：拿这句话去试问，答对了再存为评测用例。 */
  onAddToEvaluation: (question: string) => void;
}) {
  const queryClient = useQueryClient();
  /**
   * 收件箱：列表就是待办，处理一条少一条。
   *
   * 早先这里有六个控件——三个状态页签（待处理/已处理/已忽略）加三个筛选胶囊
   * （全部/能直接补进词典/要先诊断）——没有一个在做有效分流：已处理加已忽略
   * 合计 11 条、占 6% 数据，进去之后没有任何可执行的操作，是只读墓地；而
   * 79% 的记录都有落点，点「能直接补进词典」和点「全部」几乎是同一个列表。
   *
   * 现在只剩一个开关：看待办，还是看归档。已处理与已忽略合并成「已归档」，
   * 因为对建模者来说它们是同一件事——我不用再看它了；区分这两者是系统内部
   * 的记账。
   */
  const [view, setView] = useState<"open" | "archived">("open");
  const [offset, setOffset] = useState(0);
  const failures = useQuery({
    queryKey: ["analytics-query-failures", projectId, view, offset],
    queryFn: () =>
      listQueryFailures(projectId, { limit: PAGE_SIZE, offset, status: view }),
  });
  const total = failures.data?.total ?? 0;

  const mark = useMutation({
    // 按说法归档，不按 id：同一个说法的记录散在多页，拿当前页那批 id 去改只归掉
    // 看得见的几条，翻页回来它还在、次数还变了。
    mutationFn: (input: { row: FeedbackRow; next: AnalyticsFeedbackStatus }) =>
      updateQueryFailureStatus(projectId, {
        kind: input.row.kind,
        phrase: input.row.phrase,
        resolution: input.row.resolution,
        question: input.row.question,
        status: input.next,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["analytics-query-failures", projectId] });
    },
  });

  const catalog: FeedbackCatalogIndex = useMemo(
    () => ({
      metrics: revision.semantic_spec.metrics,
      dimensions: revision.semantic_spec.dimensions,
      dimensionValues: revision.semantic_catalog.dimensionValues,
    }),
    [revision.semantic_catalog.dimensionValues, revision.semantic_spec],
  );

  const rows = useMemo(
    () =>
      feedbackRows(failures.data?.items ?? []).map((row) => ({
        row,
        fix: feedbackFixTarget(row, catalog),
      })),
    [catalog, failures.data],
  );
  if (failures.isPending) return <Spinner label="加载中" />;

  return (
    <div
      className={`grid h-full grid-cols-[minmax(0,1fr)_300px] ${ANALYTICS_TASK_PANEL_CLASS}`}
    >
      <section className="min-w-0 px-6 py-5">
        <div className="flex items-start justify-between gap-4">
          <Header view={view} total={total} />
          {/* 归档不是一个页签，是一个抽屉：平时收着，需要找回误处理的记录时才打开。 */}
          <button
            type="button"
            className="shrink-0 rounded-md px-2.5 py-1 text-xs text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-700"
            onClick={() => {
              setView(view === "open" ? "archived" : "open");
              setOffset(0);
            }}
          >
            {/*
              这里曾经带一个数字，取的是后端 total——**行数**；而点进去看到的是
              前端把这些行按说法聚合之后的**种数**（实测 15 行归成 4 种）。同一
              个入口给出两个数，读者只会认为其中一个是错的。种数要等聚合下沉到
              SQL 才拿得到，在那之前宁可不写：一个会说假话的数字不如没有。
            */}
            {view === "open" ? "已归档" : "← 回到待办"}
          </button>
        </div>

        <ul className="mt-4 overflow-hidden rounded-lg border border-slate-200">
          {rows.map(({ row, fix }) => (
            <li
              key={row.key}
              className="flex items-start gap-4 border-b border-slate-100 px-4 py-3.5 last:border-b-0"
            >
              <div className="w-10 shrink-0 pt-0.5 text-center">
                <div className="text-lg font-semibold tabular-nums text-slate-700">
                  {row.count}
                </div>
                <div className="text-[10px] text-slate-400">次</div>
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-sm leading-relaxed text-slate-900">
                  「{row.question}」
                </div>
                {/*
                  第二行的顺序就是读者的关注顺序：**怎么收场的 → 落到哪个成员 →
                  用户原话怎么说**。徽章留在行首而不是挪到问句右侧：左边的次数列
                  宽度固定，所有徽章因此天然落在同一条竖线上，既成列又不受问句
                  长短影响。

                  这一行原先还夹着一句完整的说明（"没有匹配到这个说法，模型自己
                  选了「销售金额」"），和徽章、落点讲的是同一件事，成员名还重复
                  一遍——挤得说法和落点没地方站，稍长的条目就折成三行。现在只有
                  徽章说不了的信息才会出现在这里（不认识的取值、拒答原因）。
                */}
                <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-500">
                  <Badge tone={KIND_TONE[row.kind]}>{KIND_LABEL[row.kind]}</Badge>
                  {row.resolution ? (
                    <span>
                      {/* 说法在前、成员在后，中间一个等号——这正是待会儿要写进
                          词典的那条记录的样子。说法原先缀在行尾，可它是主语：
                          「毛利如何」被理解成了销售金额，读起来才顺。 */}
                      {row.phrase && (
                        <>
                          <span className="font-medium text-slate-600">
                            「{row.phrase}」
                          </span>
                          <span className="text-slate-400">= </span>
                        </>
                      )}
                      <span className="font-medium text-slate-700">{row.resolution}</span>
                      {/* 落点算不出来时要说清是为什么，否则读者看到一个成员名却
                          没有「补进词典」按钮，不知道该怪谁。 */}
                      {fix ? (
                        <span className="text-slate-400">（{TARGET_LABEL[fix.kind]}）</span>
                      ) : (
                        <span className="text-slate-400">（当前版本里没有这个成员）</span>
                      )}
                    </span>
                  ) : (
                    <>
                      <span className="text-slate-400">没有落点，要先诊断</span>
                      {/* 没有落点时说法无处可等号，但它仍是这一行的聚合依据，
                          不显示就会有几行看起来一模一样。 */}
                      {row.phrase && (
                        <span className="text-slate-400">
                          · 说法
                          <span className="font-medium text-slate-600">「{row.phrase}」</span>
                        </span>
                      )}
                    </>
                  )}
                  {row.what && <span>{row.what}</span>}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-1.5 pt-0.5">
                {/*
                  点赞行的落点是评测集，不是词典：它不缺词，是一条被人确认过的
                  问答。但评测用例的期望有二十多个字段，凭一句话空填必然是假用例，
                  所以这里只把问题带去「问数验证」自动试问一次，由建模者看过结果
                  再按「答对了？存为评测用例」——期望取的是那次真实解析。
                */}
                {row.kind === "liked" ? (
                  <Button
                    size="sm"
                    variant="primary"
                    icon={<ClipboardCheck className="h-3.5 w-3.5" />}
                    onClick={() => onAddToEvaluation(row.question)}
                  >
                    加入评测集
                  </Button>
                ) : null}
                {fix ? (
                  /*
                    title 挂在外面这层 span 上，不是按钮上：原生 disabled 的
                    <button> 不派发鼠标事件，浏览器不会为它弹 tooltip——只读时
                    用户看到一排灰按钮却读不到任何解释。span 没有 disabled，
                    hover 照常触发。
                  */
                  <span
                    className="inline-block"
                    title={
                      readOnly
                        ? "当前版本已发布、内容只读。点右上角「基于此版本继续编辑」派生草稿后即可补进词典。"
                        : undefined
                    }
                  >
                    <Button
                      size="sm"
                      variant={readOnly ? "default" : "primary"}
                      disabled={readOnly}
                      icon={<BookOpenText className="h-3.5 w-3.5" />}
                      onClick={() => onFixInDictionary(fix)}
                    >
                      补进词典
                    </Button>
                  </span>
                ) : null}
                {/* 归档不是删除——这份数据还是补词典的依据，也是"这一版比上一版好"
                    的素材。所以归档里始终能恢复。 */}
                {view === "open" ? (
                  <button
                    type="button"
                    title="不打算处理，收进归档"
                    aria-label="收进归档"
                    className="grid h-7 w-7 place-items-center rounded-md text-slate-300 transition-colors hover:bg-slate-100 hover:text-slate-600"
                    onClick={() => mark.mutate({ row, next: "ignored" })}
                  >
                    <X className="h-4 w-4" />
                  </button>
                ) : (
                  <button
                    type="button"
                    className="rounded-md px-2 py-1 text-[11px] text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-700"
                    onClick={() => mark.mutate({ row, next: "open" })}
                  >
                    恢复
                  </button>
                )}
              </div>
            </li>
          ))}
          {rows.length === 0 &&
            /* 空态只换列表内容。页签、筛选和统计留在原地——否则点进一个恰好为空的
               页签，连"切回去"的入口都跟着消失了。 */
            (
              <li className="px-4 py-12">
                <Empty {...feedbackEmptyCopy(view)} />
              </li>
            )}
        </ul>

        {total > 0 && (
          <div className="mt-2 flex items-center justify-between text-xs text-slate-500">
            {/* 说清"还剩多少条待处理"——那正是这个页面存在的意义。 */}
            <span>
              共 {total} 条，当前第 {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} 条
            </span>
            <span className="flex gap-2">
              <button
                type="button"
                className="disabled:text-slate-300"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                上一页
              </button>
              <button
                type="button"
                className="disabled:text-slate-300"
                disabled={offset + PAGE_SIZE >= total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                下一页
              </button>
            </span>
          </div>
        )}
      </section>

      <aside className="border-l border-slate-100 px-4 py-5">
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
          怎么处理
        </div>
        <ol className="rounded-lg border border-slate-200 p-3">
          {[
            "先看「用户替系统补了答案」——正解已经在那儿了，照着补别名即可。",
            "再看「模型自己猜的」里次数高的。猜对了也要补：下次可能猜错。",
            "「要先诊断」多半是关系路径缺失或事实根不对，回实体与关系改。",
          ].map((text, index) => (
            <li key={index} className="flex gap-2.5 py-1.5">
              <span className="grid h-[18px] w-[18px] shrink-0 place-items-center rounded-full bg-blue-50 text-[10px] font-semibold text-blue-700">
                {index + 1}
              </span>
              <span className="text-[11px] leading-relaxed text-slate-500">
                {text}
              </span>
            </li>
          ))}
        </ol>

        <div className="mb-2 mt-5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
          这一版的词典
        </div>
        <div className="rounded-lg border border-slate-200 px-3 py-2.5 text-xs text-slate-600">
          <div className="flex items-center justify-between">
            <span>业务术语</span>
            <span className="tabular-nums font-medium text-slate-800">
              {revision.semantic_catalog.terms.length}
            </span>
          </div>
          <div className="mt-1.5 flex items-center justify-between">
            <span>维度值</span>
            <span className="tabular-nums font-medium text-slate-800">
              {revision.semantic_catalog.dimensionValues.length}
            </span>
          </div>
        </div>

        <div className="mt-4 rounded-lg border border-dashed border-slate-300 px-3 py-2.5 text-[11px] leading-relaxed text-slate-500">
          补进词典写的是草稿版本，发布后才对线上生效。一次线上提问不是长期业务口径。
        </div>
      </aside>
    </div>
  );
}

/**
 * 标题区顺带说清"这一批有多少"。
 *
 * 这几个数原先是三张统计卡，占掉一整行；但其中两个（说法种数、能补词典的种数）
 * 和下面的筛选胶囊一字不差地重复，读者要在同一屏里把 25 和 25、23 和 23 对上，
 * 才能确认它们说的是一回事。数字留在胶囊上（那里它还兼作筛选入口），这里只留
 * 一句话。
 */
function Header({ view, total }: { view: "open" | "archived"; total: number }) {
  return (
    <header>
      <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
        <MessageSquareText className="h-4 w-4 text-slate-400" />
        {view === "open" ? "用户这么说，系统没直接听懂" : "已归档"}
      </h2>
      <p className="mt-1 max-w-2xl text-xs leading-relaxed text-slate-500">
        {view === "open" ? (
          <>
            线上真实提问回流到建模。这一页只回答一个问题：下一版该补哪些说法。
            {/* 种数由 SQL 聚合后给出，跨页也是对的。此前聚合在前端、只看得到当前
                这一页，同一个说法跨页会被重新算成一条，报出来的数字必然偏小。 */}
            {total > 0 && <> 还有 {total} 种说法待处理。</>}
          </>
        ) : (
          <>处理过和忽略掉的都在这里。想要哪条回到待办，点「恢复」。</>
        )}
      </p>
    </header>
  );
}

