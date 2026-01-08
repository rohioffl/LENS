import json
import uuid
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone

from inventory.models import ChatConversation, ChatMessage
from inventory.services.chatbot_service import chatbot_service


def _strip_redundant_greeting(text: str) -> str:
    import re

    cleaned = text.lstrip()
    pattern = re.compile(r"^(?:\s*)(hello|hi|hey|ok|okay)[^.!?\n]*[.!?\n]+\s*", re.IGNORECASE)
    # Remove leading greeting sentences repeatedly
    while True:
        new_cleaned = pattern.sub("", cleaned)
        if new_cleaned == cleaned:
            break
        cleaned = new_cleaned.lstrip()
    return cleaned


def _drop_greeting_sentences(text: str) -> str:
    import re

    sentences = re.split(r"(?<=[.!?\n])\s+", text)
    keep = []
    for s in sentences:
        lower = s.lower()
        if any(word in lower for word in ("hello", "hi ", "hey ", " ok", "okay")):
            continue
        if s.strip():
            keep.append(s.strip())
    return " ".join(keep).strip()


@csrf_exempt
def chat_send_message(request):
    """API endpoint to send a chat message and get a response"""
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)
    
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)
    
    message = payload.get("message", "").strip()
    session_id = payload.get("session_id", "").strip()
    
    if not message:
        return JsonResponse({"error": "Message is required."}, status=400)
    
    # Create or get conversation
    if not session_id:
        session_id = str(uuid.uuid4())
        conversation = ChatConversation.objects.create(session_id=session_id)
    else:
        conversation, _ = ChatConversation.objects.get_or_create(session_id=session_id)
    
    # Save user message
    user_message = ChatMessage.objects.create(
        conversation=conversation,
        role='user',
        content=message
    )
    
    # Get conversation history
    messages = []
    for msg in conversation.messages.all():
        messages.append({
            'role': msg.role,
            'content': msg.content
        })
    
    # Get AI response
    assistant_response = chatbot_service.get_completion(messages)
    assistant_response = _strip_redundant_greeting(assistant_response)
    assistant_response = _drop_greeting_sentences(assistant_response)
    
    # Save assistant message
    ChatMessage.objects.create(
        conversation=conversation,
        role='assistant',
        content=assistant_response
    )
    
    # Update conversation timestamp
    conversation.updated_at = timezone.now()
    conversation.save()
    
    return JsonResponse({
        "session_id": session_id,
        "response": assistant_response,
        "timestamp": timezone.now().isoformat()
    })


@csrf_exempt
def chat_send_message_stream(request):
    """API endpoint to send a chat message and get a streaming response"""
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)
    
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)
    
    message = payload.get("message", "").strip()
    session_id = payload.get("session_id", "").strip()
    
    if not message:
        return JsonResponse({"error": "Message is required."}, status=400)
    
    # Create or get conversation
    if not session_id:
        session_id = str(uuid.uuid4())
        conversation = ChatConversation.objects.create(session_id=session_id)
    else:
        conversation, _ = ChatConversation.objects.get_or_create(session_id=session_id)
    
    # Save user message
    ChatMessage.objects.create(
        conversation=conversation,
        role='user',
        content=message
    )
    
    # Get conversation history
    messages = []
    for msg in conversation.messages.all():
        messages.append({
            'role': msg.role,
            'content': msg.content
        })
    
    def event_stream():
        """Generator for streaming response"""
        # Send session_id first
        yield (json.dumps({
            "type": "session",
            "session_id": session_id
        }) + "\n").encode('utf-8')
        
        # Stream the response
        full_response = []
        first_chunk = True
        for chunk in chatbot_service.get_streaming_completion(messages):
            cleaned_chunk = _strip_redundant_greeting(chunk) if first_chunk else chunk
            if first_chunk:
                cleaned_chunk = _drop_greeting_sentences(cleaned_chunk)
            if cleaned_chunk:
                full_response.append(cleaned_chunk)
                yield (json.dumps({
                    "type": "chunk",
                    "content": cleaned_chunk
                }) + "\n").encode('utf-8')
            first_chunk = False
        
        # Save complete assistant message
        complete_response = ''.join(full_response)
        ChatMessage.objects.create(
            conversation=conversation,
            role='assistant',
            content=complete_response
        )
        
        # Update conversation timestamp
        conversation.updated_at = timezone.now()
        conversation.save()
        
        # Send completion signal
        yield (json.dumps({
            "type": "done",
            "timestamp": timezone.now().isoformat()
        }) + "\n").encode('utf-8')
    
    return StreamingHttpResponse(
        event_stream(),
        content_type="application/x-ndjson"
    )


@csrf_exempt
def chat_get_history(request):
    """API endpoint to get chat history for a session"""
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)
    
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)
    
    session_id = payload.get("session_id", "").strip()
    
    if not session_id:
        return JsonResponse({"error": "session_id is required."}, status=400)
    
    try:
        conversation = ChatConversation.objects.get(session_id=session_id)
    except ChatConversation.DoesNotExist:
        return JsonResponse({"messages": []})
    
    messages = [
        {
            "role": msg.role,
            "content": msg.content,
            "timestamp": msg.created_at.isoformat()
        }
        for msg in conversation.messages.all()
    ]
    
    return JsonResponse({"messages": messages})


@csrf_exempt
def chat_clear_history(request):
    """API endpoint to clear chat history for a session"""
    if request.method != "POST":
        return JsonResponse({"error": "Only POST is allowed."}, status=405)
    
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)
    
    session_id = payload.get("session_id", "").strip()
    
    if not session_id:
        return JsonResponse({"error": "session_id is required."}, status=400)
    
    try:
        conversation = ChatConversation.objects.get(session_id=session_id)
        conversation.messages.all().delete()
        return JsonResponse({"status": "ok", "message": "Chat history cleared."})
    except ChatConversation.DoesNotExist:
        return JsonResponse({"status": "ok", "message": "No conversation found."})


