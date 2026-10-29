"""POST /investigate：对一个模拟故障场景运行 Agent，返回 RCA 报告和调查过程。

mode 参数让我们用同一个接口对比两种实现：
  loop - 手写的 agent loop（app/agent/loop.py）
  sdk  - SDK 的 AFC 自动循环（app/agent/sdk_afc.py）
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agent.common import AgentError
from app.agent.loop import InvestigationAgent
from app.agent.sdk_afc import SdkAfcAgent
from app.agent.tools import build_investigation_tools
from app.api.chat import get_llm_client
from app.config import get_settings
from app.llm.base import LLMClient, LLMError
from app.llm.gemini import GeminiClient
from app.schemas import RCAReport
from app.scenario import load_scenario

router = APIRouter()
logger = logging.getLogger(__name__)


class InvestigateRequest(BaseModel):
    # pattern 在请求进来的第一道关口就拒绝非法场景名（返回 422）。
    # load_scenario 里还有一次同样的检查：多一层防御，不依赖调用方记得校验。
    scenario: str = Field(default="bug-1042", pattern=r"^[a-z0-9-]+$", max_length=64)
    mode: Literal["loop", "sdk"] = "loop"


class StepOut(BaseModel):
    step: int
    tool: str
    args: dict
    ok: bool
    result_preview: str


class InvestigateResponse(BaseModel):
    mode: str
    report: RCAReport
    steps: list[StepOut]
    llm_calls: int
    input_tokens: int
    output_tokens: int
    latency_ms: int


@router.post("/investigate", response_model=InvestigateResponse)
async def investigate(
    request: InvestigateRequest, llm: LLMClient = Depends(get_llm_client)
) -> InvestigateResponse:
    try:
        scenario = load_scenario(request.scenario)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"scenario not found: {request.scenario}")

    registry = build_investigation_tools(scenario)
    max_steps = get_settings().agent_max_steps

    if request.mode == "sdk":
        if not isinstance(llm, GeminiClient):
            raise HTTPException(status_code=400, detail="sdk mode requires the Gemini provider")
        agent = SdkAfcAgent(llm, registry, max_remote_calls=max_steps)
    else:
        agent = InvestigationAgent(llm, registry, max_steps=max_steps)

    try:
        result = await agent.investigate(scenario.ticket.id)
    except LLMError:
        logger.exception("llm call failed during investigation")
        raise HTTPException(status_code=502, detail="LLM provider error")
    except AgentError as exc:
        # 超过步数、模型不提交报告等属于"我们自己的 Agent 没能完成任务"，返回 500；
        # 具体原因只写进日志，不返回给调用方。
        logger.error("investigation failed", extra={"reason": str(exc)})
        raise HTTPException(status_code=500, detail="investigation did not complete")

    return InvestigateResponse(
        mode=request.mode,
        report=result.report,
        steps=[StepOut(**vars(s)) for s in result.steps],
        llm_calls=result.llm_calls,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
    )
