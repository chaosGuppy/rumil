import { redirect } from "next/navigation";

export default async function QuestionPage({
  params,
}: {
  params: Promise<{ questionId: string }>;
}) {
  const { questionId } = await params;
  redirect(`/pages/${questionId}`);
}
