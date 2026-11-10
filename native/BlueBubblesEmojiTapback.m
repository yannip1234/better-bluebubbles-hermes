//
// BlueBubblesEmojiTapback.m
// Runtime swizzle for BlueBubblesHelper.dylib enabling arbitrary emoji tapbacks
// (IMAssociatedMessageTypeAcknowledgmentEmoji = 2006 / 3006) on macOS Sequoia+.
//

#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <os/log.h>
#import <pthread.h>

static const long long kIMAssociatedMessageTypeEmojiAdded = 2006;
static const long long kIMAssociatedMessageTypeEmojiRemoved = 3006;

static pthread_mutex_t g_emojiMutex = PTHREAD_MUTEX_INITIALIZER;
static NSString *g_pendingEmoji = nil;

static IMP g_origParseReactionType = NULL;
static IMP g_origReactionToVerb = NULL;
static IMP g_origInitWithSender = NULL;

static BOOL isClassicReaction(NSString *reactionType) {
    if (!reactionType || ![reactionType isKindOfClass:[NSString class]]) {
        return NO;
    }
    static NSSet<NSString *> *classicSet = nil;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        classicSet = [NSSet setWithArray:@[
            @"love", @"like", @"dislike", @"laugh", @"emphasize", @"question",
            @"-love", @"-like", @"-dislike", @"-laugh", @"-emphasize", @"-question"
        ]];
    });
    return [classicSet containsObject:[reactionType lowercaseString]];
}

static NSString *extractEmojiAndMode(NSString *reactionType, BOOL *outIsRemoved) {
    if (outIsRemoved) {
        *outIsRemoved = NO;
    }
    if (!reactionType || ![reactionType isKindOfClass:[NSString class]] || reactionType.length == 0) {
        return nil;
    }
    if ([reactionType hasPrefix:@"-"] && reactionType.length > 1) {
        if (outIsRemoved) {
            *outIsRemoved = YES;
        }
        return [reactionType substringFromIndex:1];
    }
    return reactionType;
}

static long long swizzled_parseReactionType(id self, SEL _cmd, NSString *reactionType) {
    if (isClassicReaction(reactionType)) {
        pthread_mutex_lock(&g_emojiMutex);
        g_pendingEmoji = nil;
        pthread_mutex_unlock(&g_emojiMutex);
        return ((long long(*)(id, SEL, NSString *))g_origParseReactionType)(self, _cmd, reactionType);
    }

    BOOL isRemoved = NO;
    NSString *emoji = extractEmojiAndMode(reactionType, &isRemoved);
    if (emoji.length > 0) {
        pthread_mutex_lock(&g_emojiMutex);
        g_pendingEmoji = [emoji copy];
        pthread_mutex_unlock(&g_emojiMutex);
        return isRemoved ? kIMAssociatedMessageTypeEmojiRemoved : kIMAssociatedMessageTypeEmojiAdded;
    }

    return ((long long(*)(id, SEL, NSString *))g_origParseReactionType)(self, _cmd, reactionType);
}

static NSString *swizzled_reactionToVerb(id self, SEL _cmd, NSString *reactionType) {
    if (isClassicReaction(reactionType)) {
        return ((NSString *(*)(id, SEL, NSString *))g_origReactionToVerb)(self, _cmd, reactionType);
    }

    BOOL isRemoved = NO;
    NSString *emoji = extractEmojiAndMode(reactionType, &isRemoved);
    if (emoji.length > 0) {
        pthread_mutex_lock(&g_emojiMutex);
        g_pendingEmoji = [emoji copy];
        pthread_mutex_unlock(&g_emojiMutex);
        if (isRemoved) {
            return [NSString stringWithFormat:@"Removed %@ from ", emoji];
        }
        return [NSString stringWithFormat:@"Reacted %@ to ", emoji];
    }

    return ((NSString *(*)(id, SEL, NSString *))g_origReactionToVerb)(self, _cmd, reactionType);
}

static NSString *extractEmojiFromReactionText(NSAttributedString *text, long long associatedMessageType) {
    if (!text || ![text respondsToSelector:@selector(string)]) {
        return nil;
    }
    NSString *raw = [text string];
    if (!raw || raw.length == 0) {
        return nil;
    }
    if (associatedMessageType == kIMAssociatedMessageTypeEmojiAdded && [raw hasPrefix:@"Reacted "]) {
        NSRange toRange = [raw rangeOfString:@" to "];
        if (toRange.location != NSNotFound && toRange.location > 8) {
            return [raw substringWithRange:NSMakeRange(8, toRange.location - 8)];
        }
    } else if (associatedMessageType == kIMAssociatedMessageTypeEmojiRemoved && [raw hasPrefix:@"Removed "]) {
        NSRange fromRange = [raw rangeOfString:@" from "];
        if (fromRange.location != NSNotFound && fromRange.location > 8) {
            return [raw substringWithRange:NSMakeRange(8, fromRange.location - 8)];
        }
    }
    return nil;
}

static id swizzled_initWithSender(
    id self,
    SEL _cmd,
    id sender,
    id time,
    NSAttributedString *text,
    id messageSubject,
    id fileTransferGUIDs,
    unsigned long long flags,
    id error,
    id guid,
    id subject,
    id associatedMessageGUID,
    long long associatedMessageType,
    NSRange associatedMessageRange,
    id messageSummaryInfo
) {
    id msg = ((id(*)(
        id, SEL, id, id, id, id, id, unsigned long long, id, id, id, id, long long, NSRange, id
    ))g_origInitWithSender)(
        self,
        _cmd,
        sender,
        time,
        text,
        messageSubject,
        fileTransferGUIDs,
        flags,
        error,
        guid,
        subject,
        associatedMessageGUID,
        associatedMessageType,
        associatedMessageRange,
        messageSummaryInfo
    );

    if (msg && (associatedMessageType == kIMAssociatedMessageTypeEmojiAdded ||
                associatedMessageType == kIMAssociatedMessageTypeEmojiRemoved)) {
        NSString *emoji = extractEmojiFromReactionText(text, associatedMessageType);
        if (!emoji || emoji.length == 0) {
            pthread_mutex_lock(&g_emojiMutex);
            emoji = [g_pendingEmoji copy];
            pthread_mutex_unlock(&g_emojiMutex);
        }
        SEL setEmojiSel = sel_registerName("_associatedMessageEmoji:");
        if (emoji.length > 0 && [msg respondsToSelector:setEmojiSel]) {
            ((void(*)(id, SEL, id))objc_msgSend)(msg, setEmojiSel, emoji);
            os_log_t log = os_log_create("com.bluebubbles.messaging", "DEBUG");
            os_log(log, "BLUEBUBBLESHELPER: Attached custom associatedMessageEmoji '%{public}@' (type=%lld)",
                   emoji, associatedMessageType);
        }
    }

    return msg;
}

__attribute__((constructor))
static void initBlueBubblesEmojiTapbackSwizzle(void) {
    @autoreleasepool {
        os_log_t log = os_log_create("com.bluebubbles.messaging", "DEBUG");

        Class helperMeta = objc_getMetaClass("BlueBubblesHelper");
        if (helperMeta) {
            Method parseMethod = class_getClassMethod(objc_getClass("BlueBubblesHelper"),
                                                      sel_registerName("parseReactionType:"));
            if (parseMethod) {
                g_origParseReactionType = method_setImplementation(parseMethod, (IMP)swizzled_parseReactionType);
            }
            Method verbMethod = class_getClassMethod(objc_getClass("BlueBubblesHelper"),
                                                     sel_registerName("reactionToVerb:"));
            if (verbMethod) {
                g_origReactionToVerb = method_setImplementation(verbMethod, (IMP)swizzled_reactionToVerb);
            }
        }

        Class imMessageCls = objc_getClass("IMMessage");
        SEL initSel = sel_registerName(
            "initWithSender:time:text:messageSubject:fileTransferGUIDs:flags:error:guid:subject:"
            "associatedMessageGUID:associatedMessageType:associatedMessageRange:messageSummaryInfo:"
        );
        if (imMessageCls) {
            Method initMethod = class_getInstanceMethod(imMessageCls, initSel);
            if (initMethod) {
                g_origInitWithSender = method_setImplementation(initMethod, (IMP)swizzled_initWithSender);
            }
        }

        os_log(log, "BLUEBUBBLESHELPER: Emoji tapback swizzle initialized (parse=%d, verb=%d, immsg=%d)",
               g_origParseReactionType != NULL,
               g_origReactionToVerb != NULL,
               g_origInitWithSender != NULL);
    }
}
