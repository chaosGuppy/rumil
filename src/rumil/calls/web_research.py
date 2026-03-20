"""Web research call: search the web and create source-grounded claims."""

import json
import logging
from collections.abc import Sequence

import anthropic
from anthropic.types import ServerToolUseBlock, ToolUseBlock

from rumil.calls.base import BaseCall
from rumil.calls.common import (
    _execute_tool_uses,
    _prepare_tools,
    _record_round_moves,
    format_moves_for_review,
    log_page_ratings,
    resolve_page_refs,
    run_closing_review,
)
from rumil.context import build_embedding_based_context
from rumil.database import DB
from rumil.llm import (
    LLMExchangeMetadata,
    Tool,
    build_system_prompt,
    build_user_message,
    call_api,
)
from rumil.models import (
    Call,
    CallStage,
    CallType,
    MoveType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.moves.base import write_page_file
from rumil.moves.registry import MOVES
from rumil.settings import get_settings
from rumil.tracing.trace_events import ContextBuiltEvent, ReviewCompleteEvent

log = logging.getLogger(__name__)

WEB_RESEARCH_MOVES = [
    MoveType.CREATE_CLAIM,
    MoveType.LINK_CONSIDERATION,
    MoveType.LOAD_PAGE,
]


class WebResearchCall(BaseCall):
    """Web research call: search and fetch web sources, create grounded claims."""

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        allowed_domains: Sequence[str] | None = None,
        broadcaster=None,
        up_to_stage: CallStage | None = None,
    ):
        super().__init__(
            question_id, call, db,
            broadcaster=broadcaster, up_to_stage=up_to_stage,
        )
        self.allowed_domains = allowed_domains
        self.source_page_ids: dict[str, str] = {}

    async def build_context(self) -> None:
        question = await self.db.get_page(self.question_id)
        query = question.headline if question else self.question_id
        emb_result = await build_embedding_based_context(
            query, self.db, scope_question_id=self.question_id,
        )
        self.working_page_ids = emb_result.page_ids
        self.context_text = emb_result.context_text

        system_prompt = build_system_prompt('web_research')
        user_message = build_user_message(self.context_text, '(diagnostic)')
        log.debug(
            'Web research context diagnostic: '
            'context_text=%d chars, system_prompt=%d chars, '
            'user_message=%d chars, total_prompt=%d chars, '
            'full_pages=%d, abstract_pages=%d, summary_pages=%d, '
            'distillation_pages=%d, '
            'budget_usage=%s',
            len(self.context_text), len(system_prompt),
            len(user_message), len(system_prompt) + len(user_message),
            len(emb_result.full_page_ids), len(emb_result.abstract_page_ids),
            len(emb_result.summary_page_ids), len(emb_result.distillation_page_ids),
            emb_result.budget_usage,
        )

        await self.trace.record(ContextBuiltEvent(
            working_context_page_ids=await resolve_page_refs(
                self.working_page_ids, self.db,
            ),
            preloaded_page_ids=[],
        ))

    async def create_pages(self) -> None:
        settings = get_settings()
        max_rounds = 2 if settings.is_smoke_test else 5
        client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())

        server_tools = self._build_server_tools()
        custom_tools = [MOVES[mt].bind(self.state) for mt in WEB_RESEARCH_MOVES]
        custom_tools = self._wrap_create_claim(custom_tools)
        custom_tool_defs, custom_tool_fns = _prepare_tools(custom_tools)
        all_tool_defs: list = server_tools + custom_tool_defs

        system_prompt = build_system_prompt('web_research')
        task = (
            'Search the web for evidence relevant to this question and create '
            'source-grounded claims.\n\n'
            f'Question ID (use this when linking considerations): '
            f'`{self.question_id}`'
        )
        user_message = build_user_message(self.context_text, task)
        messages: list[dict] = [{'role': 'user', 'content': user_message}]

        log.debug(
            'Web research create_pages starting: '
            'system_prompt=%d chars, user_message=%d chars, '
            'server_tools=%d, custom_tools=%d, all_tool_defs=%d',
            len(system_prompt), len(user_message),
            len(server_tools), len(custom_tool_defs), len(all_tool_defs),
        )
        tool_defs_chars = len(json.dumps(all_tool_defs))
        log.debug(
            'Tool definitions total: %d chars (%d tokens approx)',
            tool_defs_chars, tool_defs_chars // 4,
        )

        for round_num in range(max_rounds):
            total_msg_chars = sum(
                len(str(m.get('content', ''))) for m in messages
            )
            log.debug(
                'Round %d: %d messages, ~%d chars in messages',
                round_num, len(messages), total_msg_chars,
            )
            meta = LLMExchangeMetadata(
                call_id=self.call.id, phase='web_research_loop',
                trace=self.trace, round_num=round_num,
                user_message=user_message if round_num == 0 else None,
            )
            api_resp = await call_api(
                client, settings.model, system_prompt, messages,
                all_tool_defs, max_tokens=4096,
                metadata=meta, db=self.db,
            )
            response = api_resp.message

            custom_tool_uses: list[ToolUseBlock] = []
            for block in response.content:
                if isinstance(block, ToolUseBlock):
                    custom_tool_uses.append(block)

            messages.append({'role': 'assistant', 'content': response.content})

            if custom_tool_uses:
                tool_calls, tool_results = await _execute_tool_uses(
                    custom_tool_uses, custom_tool_fns,
                )
                await _record_round_moves(
                    trace=self.trace, state=self.state, db=self.db,
                )
                messages.append({'role': 'user', 'content': tool_results})

            if response.stop_reason == 'end_turn' or not (
                custom_tool_uses
                or any(
                    isinstance(b, ServerToolUseBlock) for b in response.content
                )
            ):
                break

        log.info(
            'Web research create_pages complete: %d pages created, %d sources',
            len(self.state.created_page_ids), len(self.source_page_ids),
        )

    async def closing_review(self) -> None:
        review_context = format_moves_for_review(self.state.moves)
        review = await run_closing_review(
            self.call,
            review_context,
            self.context_text,
            loaded_page_ids=[],
            created_page_ids=self.state.created_page_ids,
            db=self.db,
            trace=self.trace,
        )
        if review:
            log.info(
                'Web research review: confidence=%s',
                review.get('confidence_in_output', '?'),
            )
            await log_page_ratings(review, self.db)
            await self.trace.record(ReviewCompleteEvent(
                remaining_fruit=review.get('remaining_fruit'),
                confidence=review.get('confidence_in_output'),
            ))
        self.review = review or {}

    def result_summary(self) -> str:
        return (
            f'Web research complete. {len(self.state.created_page_ids)} claims created, '
            f'{len(self.source_page_ids)} sources cited.'
        )

    def _build_server_tools(self) -> list[dict]:
        web_search: dict = {
            'type': 'web_search_20250305',
            'name': 'web_search',
        }
        if self.allowed_domains:
            web_search['allowed_domains'] = list(self.allowed_domains)

        web_fetch: dict = {
            'type': 'web_fetch_20250910',
            'name': 'web_fetch',
            'max_content_tokens': 20000,
        }
        if self.allowed_domains:
            web_fetch['allowed_domains'] = list(self.allowed_domains)

        return [web_search, web_fetch]

    async def _ensure_source_page(self, url: str) -> str | None:
        """Create a source page for a URL if one doesn't exist yet."""
        if url in self.source_page_ids:
            return self.source_page_ids[url]

        from rumil.scraper import scrape_url
        scraped = await scrape_url(url)
        if scraped is None:
            log.warning('Scrape failed for URL: %s, skipping citation', url)
            return None

        page = Page(
            page_type=PageType.SOURCE,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=scraped.content,
            headline=scraped.title[:120],
            epistemic_status=2.5,
            epistemic_type='web source',
            provenance_model='scraper',
            provenance_call_type=CallType.WEB_RESEARCH.value,
            provenance_call_id=self.call.id,
            extra={
                'url': url,
                'fetched_at': scraped.fetched_at,
                'char_count': len(scraped.content),
            },
        )
        await self.db.save_page(page)
        write_page_file(page)

        self.source_page_ids[url] = page.id
        self.state.created_page_ids.append(page.id)
        log.info(
            'Source page created: %s -> %s (%s)',
            url[:60], page.id[:8], scraped.title[:60],
        )
        return page.id

    def _wrap_create_claim(self, tools: list[Tool]) -> list[Tool]:
        """Wrap the create_claim tool to resolve URL source_ids into page IDs."""
        wrapped: list[Tool] = []
        for tool in tools:
            if tool.name == 'create_claim':
                original_fn = tool.fn

                async def wrapped_fn(inp: dict, _orig=original_fn) -> str:
                    source_ids = inp.get('source_ids', [])
                    if source_ids:
                        resolved: list[str] = []
                        for sid in source_ids:
                            if isinstance(sid, str) and sid.startswith('http'):
                                page_id = await self._ensure_source_page(sid)
                                if page_id:
                                    resolved.append(page_id)
                            else:
                                resolved.append(sid)
                        inp = {**inp, 'source_ids': resolved}
                    return await _orig(inp)

                wrapped.append(Tool(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    fn=wrapped_fn,
                ))
            else:
                wrapped.append(tool)
        return wrapped
