import json
import uuid
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone

from inventory.models import ChatConversation, ChatMessage
from inventory.services.chatbot_service import chatbot_service


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

