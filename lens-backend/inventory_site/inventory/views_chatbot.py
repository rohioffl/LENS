import json
import uuid
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone

from inventory.models import ChatConversation, ChatMessage
from inventory.services.chatbot_service import chatbot_service


INTRO_GREETING = "Hello, I'm Skyra. Ready to assist."


def _strip_redundant_greeting(text: str) -> str:
    cleaned = text.lstrip()
    while True:
        lowered = cleaned.lower().lstrip()
        if not lowered:
            return cleaned
        sentence_end = None
        for token in (".", "!", "?", "\n"):
            idx = cleaned.find(token)
            if idx != -1 and (sentence_end is None or idx < sentence_end):
                sentence_end = idx
        first_sentence = cleaned if sentence_end is None else cleaned[:sentence_end + 1]
        first_lower = first_sentence.lower()
        if any(word in first_lower for word in ("hello", "hi", "hey", "okay", "ok")):
            cleaned = cleaned[len(first_sentence):].lstrip()
            continue
        break
    return cleaned


def _should_add_greeting(conversation: ChatConversation) -> bool:
    return not conversation.messages.filter(role="assistant").exists()


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
    if _should_add_greeting(conversation):
        assistant_response = _strip_redundant_greeting(assistant_response)
        assistant_response = f"{INTRO_GREETING} {assistant_response}".strip()
    
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
        if _should_add_greeting(conversation):
            full_response.append(INTRO_GREETING + " ")
            yield (json.dumps({
                "type": "chunk",
                "content": INTRO_GREETING + " "
            }) + "\n").encode('utf-8')
        for chunk in chatbot_service.get_streaming_completion(messages):
            full_response.append(chunk)
            yield (json.dumps({
                "type": "chunk",
                "content": chunk
            }) + "\n").encode('utf-8')
        
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

