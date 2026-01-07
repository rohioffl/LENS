# Chatbot UI Preview

## Visual Design

```
┌─────────────────────────────────────────┐
│  🤖 AI Assistant                    🗑️  │
│  Cloud Infrastructure Expert            │
├─────────────────────────────────────────┤
│                                         │
│  ┌──────────────────────────────────┐  │
│  │ 👋 Hi! How can I help you today? │  │
│  │                                   │  │
│  │ Ask me anything about:            │  │
│  │  ✓ AWS & GCP infrastructure      │  │
│  │  ✓ Terraform & IaC               │  │
│  │  ✓ Container orchestration       │  │
│  │  ✓ Cloud migration strategies    │  │
│  │  ✓ Security best practices       │  │
│  └──────────────────────────────────┘  │
│                                         │
│  👤 User Message                        │
│  ┌─────────────────────────────────┐   │
│  │ How do I set up VPN?            │   │
│  │                         2:30 PM  │   │
│  └─────────────────────────────────┘   │
│                                         │
│  🤖 Assistant Response                  │
│  ┌─────────────────────────────────┐   │
│  │ To set up a VPN between AWS     │   │
│  │ and GCP, you have two options:  │   │
│  │ Classic VPN or HA VPN...        │   │
│  │                         2:30 PM  │   │
│  └─────────────────────────────────┘   │
│                                         │
│  🤖 ...                                 │
│  ● ● ●  (typing animation)              │
│                                         │
├─────────────────────────────────────────┤
│  ┌──────────────────────────────┐  ➤   │
│  │ Type your message...         │      │
│  └──────────────────────────────┘      │
└─────────────────────────────────────────┘

         💬  (Floating Button)
```

## Color Scheme

- **Primary Gradient**: Purple to Violet (#667eea → #764ba2)
- **User Messages**: Pink gradient (#f093fb → #f5576c)
- **Background**: Light gray (#f5f5f5)
- **Text**: Dark gray (#333) / White

## Animations

1. **Button Hover**: Scales to 110% + enhanced shadow
2. **Window Open**: Fade in + scale up + slide up
3. **Messages**: Fade in + slide up from bottom
4. **Typing Indicator**: Bouncing dots animation
5. **Button Rotation**: Rotates 180° when opening/closing

## Responsive Design

- **Desktop**: 400px × 600px window
- **Mobile**: Full screen with margins
- **Position**: Fixed bottom-right corner

## Key Interactions

1. Click chat button → Window opens with smooth animation
2. Type message → Send with Enter or click button
3. Response streams in real-time character by character
4. Click trash icon → Clear chat history (with confirmation)
5. Scroll automatically to latest message
6. Session persists across page reloads

## Accessibility

- Semantic HTML structure
- ARIA labels for screen readers
- Keyboard navigation support
- Focus management
- Smooth scrolling behavior

