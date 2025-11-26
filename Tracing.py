import asyncio
import os
from typing import Annotated
from agent_framework import ChatAgent
from agent_framework.azure import AzureAIAgentClient
from agent_framework.observability import get_tracer
from azure.ai.agents.aio import AgentsClient
from azure.ai.projects.aio import AIProjectClient
from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv
from opentelemetry.trace import SpanKind
from opentelemetry.trace.span import format_trace_id
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace.export import ConsoleSpanExporter
from pydantic import Field
from agent_framework.observability import setup_observability
from opentelemetry import trace
from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter


class FilteringSpanProcessor(SpanProcessor):
    """Custom span processor to filter traces based on criteria."""
    
    def __init__(self, exporter, filter_criteria=None):
        self.exporter = exporter
        self.filter_criteria = filter_criteria or {}
    
    def on_start(self, span, parent_context):
        """Called when a span starts."""
        pass
    
    def on_end(self, span):
        """Called when a span ends - apply filtering here."""
        # Example filters:
        
        # 1. Filter by span name pattern
        if self.filter_criteria.get("include_names"):
            if not any(name in span.name for name in self.filter_criteria["include_names"]):
                return
        
        # 2. Filter by operation type (attributes)
        if self.filter_criteria.get("operation_names"):
            operation = span.attributes.get("gen_ai.operation.name")
            if operation not in self.filter_criteria["operation_names"]:
                return
        
        # 3. Filter by minimum duration (in seconds)
        if self.filter_criteria.get("min_duration_ms"):
            duration_ns = span.end_time - span.start_time
            duration_ms = duration_ns / 1_000_000
            if duration_ms < self.filter_criteria["min_duration_ms"]:
                return
        
        # 4. Exclude certain operations
        if self.filter_criteria.get("exclude_operations"):
            operation = span.attributes.get("gen_ai.operation.name")
            if operation in self.filter_criteria["exclude_operations"]:
                return
        
        # 5. Filter by span kind
        if self.filter_criteria.get("exclude_span_kinds"):
            if span.kind in self.filter_criteria["exclude_span_kinds"]:
                return
        
        # If span passes all filters, export it
        self.exporter.export([span])
    
    def shutdown(self):
        """Shutdown the processor."""
        self.exporter.shutdown()
    
    def force_flush(self, timeout_millis=30000):
        """Force flush the processor."""
        return self.exporter.force_flush(timeout_millis)

load_dotenv()

# Azure AI Project Endpoint
AZURE_AI_PROJECT_ENDPOINT = ""


async def get_current_time(
    timezone: Annotated[str, Field(description="The timezone to get the current time for (e.g., 'UTC', 'America/New_York', 'Asia/Tokyo')")]
) -> str:
    """Get the current time for a given timezone."""
    from datetime import datetime
    import pytz
    
    try:
        tz = pytz.timezone(timezone)
        current_time = datetime.now(tz)
        return f"The current time in {timezone} is {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    except Exception as e:
        return f"Error getting time for timezone {timezone}: {str(e)}"


async def setup_azure_ai_observability(
    project_client: AIProjectClient, enable_sensitive_data: bool | None = None
) -> None:
    """Use this method to setup tracing in your Azure AI Project.

    This will take the connection string from the AIProjectClient instance.
    It will override any connection string that is set in the environment variables.
    It will disable any OTLP endpoint that might have been set.
    """
    try:
        conn_string = await project_client.telemetry.get_application_insights_connection_string()
    except ResourceNotFoundError:
        print("No Application Insights connection string found for the Azure AI Project.")
        return
    
    # Define filter criteria - customize as needed
    filter_criteria = {
        # Only show these operations (uncomment to use)
        # "operation_names": ["chat", "execute_tool"],
        
        # Only show spans with these names (uncomment to use)
        # "include_names": ["chat", "execute_tool"],
        
        # Only show spans taking longer than X milliseconds (uncomment to use)
        # "min_duration_ms": 100,
        
        # Exclude these operations (uncomment to use)
        # "exclude_operations": ["some_operation_to_exclude"],
        
        # Exclude spans by kind
        #"exclude_span_kinds": [SpanKind.CLIENT],
    }
       
    # Setup basic observability first
    setup_observability(
        applicationinsights_connection_string=conn_string, 
        enable_sensitive_data=enable_sensitive_data
    )
    
    # Get the tracer provider and add filtered Azure Monitor exporter
    tracer_provider = trace.get_tracer_provider()
    
    # Create Azure Monitor exporter with filtering
    azure_exporter = AzureMonitorTraceExporter(connection_string=conn_string)
    azure_filtering_processor = FilteringSpanProcessor(azure_exporter, filter_criteria)
    

    # Default Azure Monitor processor added by setup_observability and replaces it with our filtered version
    existing_processors = list(tracer_provider._active_span_processor._span_processors)
    for processor in existing_processors:
        if hasattr(processor, '_exporter') and isinstance(processor._exporter, AzureMonitorTraceExporter):
            tracer_provider._active_span_processor._span_processors.remove(processor)
    
    tracer_provider.add_span_processor(azure_filtering_processor)
    
    # Add console exporter with filtering for local tracing visibility
    console_exporter = ConsoleSpanExporter()
    console_filtering_processor = FilteringSpanProcessor(console_exporter, filter_criteria)
    tracer_provider.add_span_processor(console_filtering_processor)


async def main():
    async with (
        DefaultAzureCredential() as credential,
        AIProjectClient(endpoint=AZURE_AI_PROJECT_ENDPOINT, credential=credential) as project_client,
        AgentsClient(endpoint=AZURE_AI_PROJECT_ENDPOINT, credential=credential) as agents_client,
    ):
        # Setup observability with Application Insights
        await setup_azure_ai_observability(project_client, enable_sensitive_data=True)
        
        agent_client = AzureAIAgentClient(
            agents_client=agents_client,
            #agent_name="BasicAgent-AgentFramework"  
            agent_id=""              
        )
        
        with get_tracer().start_as_current_span(
            name="Agent Execution with Tracing", kind=SpanKind.CLIENT
        ) as current_span:
            print(f"Trace ID: {format_trace_id(current_span.get_span_context().trace_id)}")
            
            async with ChatAgent(chat_client=agent_client, tools=[get_current_time]) as agent:
                result = await agent.run("What is the current time in New York and Tokyo?")
                print(result.text)

asyncio.run(main())