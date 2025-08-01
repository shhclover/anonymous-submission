import json
import argparse
import os
from tqdm import tqdm
import sys
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

sys.path.append('/root/shh/FGRAG/fgrag')
from core.utils import format_arc_choices_for_prompt, postprocess_arc_answer, postprocess_popqa_answer, postprocess_pubqa_answer
from core.arc_utils import get_arc_choices, format_arc_choices_for_instruction, postprocess_arc_answer_unified, setup_arc_processing

TASK_INST = {
    "popqa": "Answer the following question based on your knowledge and any provided information.",
    "arc_challenge": "Given four answer candidates, A, B, C and D, choose the best answer choice.",
    "bio": "Generate a comprehensive biography based on your knowledge and any provided information.",
    "pubqa": "Is the following statement correct or not? Say true if it's correct; otherwise say false."
}

control_tokens = ["[Fully supported]", "[Partially supported]", "[No support / Contradictory]", 
                  "[No Retrieval]", "[Retrieval]", "[Irrelevant]", "[Relevant]", 
                  "<paragraph>", "</paragraph>", "[Utility:1]", "[Utility:2]", 
                  "[Utility:3]", "[Utility:4]", "[Utility:5]", "[Continue to Use Evidence]"]




def format_knowledge_first_prompt(item_index, task, query, consensus_text, additional_evidence_list, choices_data=None, item_choices=None):
    """
    知识优先格式：先让模型基于内部知识回答，再考虑检索信息
    """
    # 构建指令部分
    instruction = TASK_INST[task] + "\n\n## Input:\n\n" + query
    
    # 添加选项（对于ARC Challenge）
    if task == "arc_challenge":
        choices_to_use = get_arc_choices(item_choices, choices_data, item_index)
        instruction = format_arc_choices_for_instruction(choices_to_use, instruction)

    # 构建知识优先的prompt
    prompt = "### Instruction:\n{0}\n\n### Response:\n".format(instruction)
    
    # 第一步：基于内部知识的初步判断
    prompt += ("First, let me consider what I know from my training data about this question.\n\n"
              "[No Retrieval]Based on my internal knowledge: ")
    
    # 判断是否有有效的检索信息
    has_valid_consensus = (consensus_text and consensus_text.strip() and 
                          "ConsensusMissingInInput" not in consensus_text and
                          not any(marker in consensus_text.lower() for marker in ["no consensus answer", "insufficient evidenc"]))
    
    has_valid_evidence = (additional_evidence_list and isinstance(additional_evidence_list, list) and
                         any(e and e.strip() and len(e.strip()) > 20 for e in additional_evidence_list))
    
    return prompt, has_valid_consensus, has_valid_evidence


def postprocess_answer(answer, task):
    """
    改进的答案后处理
    """
    # 清理控制tokens
    for token in control_tokens:
        answer = answer.replace(token, "")
    answer = answer.replace("</s>", "").replace("\n", " ").replace("<|endoftext|>", "").strip()

    if task == "arc_challenge":
        return postprocess_arc_answer_unified(answer)
    elif task == "popqa":
        return postprocess_popqa_answer(answer)
    elif task == "pubqa":
        return postprocess_pubqa_answer(answer)
    
    return answer

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input_file', type=str, required=True, help='Input JSONL file')
    parser.add_argument('--output_file', type=str, required=True, help='Output JSONL file')
    parser.add_argument('--selfrag_model_path', type=str, required=True, help='Path to Self-RAG model')
    parser.add_argument('--task', type=str, required=True, choices=['popqa', 'arc_challenge', 'bio', 'pubqa'])
    parser.add_argument('--max_tokens', type=int, default=100, help='Maximum tokens to generate')
    parser.add_argument('--temperature', type=float, default=0.0, help='Temperature for generation')
    parser.add_argument('--device', type=str, default='cuda:0', help='Device to use')
    parser.add_argument('--strategy', type=str, default='adaptive',
                       choices=['adaptive', 'enhanced_adaptive', 'two_stage'],
                       help='Reasoning strategy: adaptive, enhanced_adaptive, or two_stage')
    parser.add_argument('--num_samples', type=int, default=-1,
                       help='Number of samples to process (-1 for all)')

    args = parser.parse_args()
    
    # 加载模型
    print(f"Loading Self-RAG model from {args.selfrag_model_path}")
    tokenizer = AutoTokenizer.from_pretrained(args.selfrag_model_path)
    model = AutoModelForCausalLM.from_pretrained(
        args.selfrag_model_path,
        torch_dtype=torch.float16,
        device_map=None
    )
    model = model.to(args.device)
    
    # 设置特殊tokens
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    
    # 处理数据
    with open(args.input_file, 'r') as f:
        data = [json.loads(line) for line in f]

    # 限制样本数量
    if args.num_samples > 0:
        data = data[:args.num_samples]

    results = []
    
    for i, item in enumerate(tqdm(data, desc=f"Processing with {args.strategy} strategy")):
        query = item['query']
        consensus_text = item.get('consensus', '')
        additional_evidence = item.get('additional_evidence', [])
        
        try:
            if args.strategy == 'adaptive':
                # 自适应策略
                prompt = format_adaptive_prompt(
                    i, args.task, query, consensus_text, additional_evidence,
                    item.get('choices_data'), item.get('choices')
                )
                answer = query_selfrag_adaptive(model, tokenizer, prompt, args.device,
                                              args.max_tokens, args.temperature)

            elif args.strategy == 'enhanced_adaptive':
                # 增强自适应策略：根据信息质量调整推理策略
                prompt = format_enhanced_adaptive_prompt(
                    i, args.task, query, consensus_text, additional_evidence,
                    item.get('choices_data'), item.get('choices')
                )
                answer = query_selfrag_adaptive(model, tokenizer, prompt, args.device,
                                              args.max_tokens, args.temperature)

            elif args.strategy == 'two_stage':
                # 两阶段策略
                base_prompt, has_consensus, has_evidence = format_knowledge_first_prompt(
                    i, args.task, query, consensus_text, additional_evidence,
                    item.get('choices_data'), item.get('choices')
                )
                answer = query_selfrag_two_stage(model, tokenizer, base_prompt, 
                                               consensus_text, additional_evidence,
                                               args.device, args.max_tokens, args.temperature)
            
            # 后处理答案
            processed_answer = postprocess_answer(answer, args.task)
            
            # 保存结果 - 使用与原始方法一致的字段名
            result = {
                'query': query,
                'processed_answer': processed_answer,  # 评估脚本期望的字段名
                'raw_selfrag_response': answer,        # 与原始方法一致
                'method': f'selfrag_{args.strategy}'
            }
            
            # 保留原始字段
            for key in ['id', 'answerKey', 'choices', 'choices_data']:
                if key in item:
                    result[key] = item[key]
            
            results.append(result)
            
        except Exception as e:
            print(f"Error processing item {i}: {e}")
            # 添加错误结果
            results.append({
                'query': query,
                'processed_answer': '',
                'raw_selfrag_response': f'Error: {str(e)}',
                'method': f'selfrag_{args.strategy}'
            })
    
    # 保存结果
    os.makedirs(os.path.dirname(args.output_file), exist_ok=True)
    with open(args.output_file, 'w') as f:
        for result in results:
            f.write(json.dumps(result) + '\n')
    
    print(f"Results saved to {args.output_file}")

if __name__ == "__main__":
    main()
